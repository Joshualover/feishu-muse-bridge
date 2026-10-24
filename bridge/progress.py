"""飞书桥接进度回执：消息一到先发占位回执，处理中原地更新，最后变成正式答复。

解决两个体验问题：
1. 延迟感：占位回执在消息入队后立刻发出（1~2 秒），用户能立刻确认桥是活的；
2. 黑盒感：处理进程把每一步进度更新到同一条回执上，最后一步替换为正式
   答复全文，用户全程看得见在干什么，不用猜是卡住还是断线。

两种回执形态（2026-10-23 引入流式卡片，移植自 muse2feishu 的打字机方案）：
- **流式卡片（优先）**：建一个 CardKit streaming_mode 卡片并发送，
  每次更新都以累计全文 + 递增 sequence PUT 到卡片元素，飞书客户端按
  70ms/字的速度打字机渲染。需要飞书应用开通 cardkit:card:write 权限。
- **整卡 PATCH（降级）**：建卡失败（缺权限等）或推送失败时，自动降级为
  旧的整卡替换流程（卡片是飞书唯一支持原地 PATCH 的消息类型，
  且其 markdown 元素能真实渲染 **加粗**；post/text 消息无法被编辑，
  2026-10-22 实测 PATCH 返回 230001 "This message is NOT a card"）。

降级是自动且静默的：流式不可用时用户看到的只是旧版进度卡，不会报错、
不会干等。流式状态记在数据目录的 streaming_state.json 里（按 queue_id），
与 SQLite 队列行解耦。

用法:
  python3 -m bridge.progress start --chat-id <chat_id> --reply-to <用户消息id> \
      [--queue-id N | --feishu-message-id om_...] [--text "⏳ 已收到…"]
  python3 -m bridge.progress update --queue-id N --text "<当前进度全文>"
  python3 -m bridge.progress finish --queue-id N --text "<最终答复全文>"
  python3 -m bridge.progress fail --queue-id N --text "<给用户的失败说明>" --error "<错误详情>"

finish/fail 在更新回执的同时完成队列 mark_done/mark_failed。
没有 progress 回执的历史消息（回执发送失败等）请回退到 send_reply 流程。
"""

import argparse
import json
import os
import pathlib
import sys
import uuid

import requests

import bridge.queue_store as store
from bridge.send_reply import get_tenant_access_token, load_credentials

REPLY_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
UPDATE_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
REACTION_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions"
CARDKIT_CREATE_URL = "https://open.feishu.cn/open-apis/cardkit/v1/cards"
CARDKIT_ELEMENT_URL = (
    "https://open.feishu.cn/open-apis/cardkit/v1/cards/{card_id}/elements/{element_id}/content"
)

DEFAULT_PLACEHOLDER = (
    "⏳ **已收到**，Edison 正在处理…\n"
    "（这张卡片会实时更新进度，最后变成正式答复）"
)

TITLE_WORKING = "🔄 Edison 正在处理"
TITLE_DONE = "✅ 处理完成"
TITLE_FAILED = "⚠️ 处理失败"

# 流式卡片里被打字机推送的 markdown 元素 id（建卡时固定，推送时引用）
STREAM_ELEMENT_ID = "progress_stream_md"
# 流式卡片打字速度：70ms/字，与 muse2feishu 的实测配置一致
PRINT_FREQUENCY_MS = 70


def build_card(text: str, title: str = TITLE_WORKING, template: str = "blue") -> str:
    """整卡 PATCH 用的静态卡片 content（降级路径 + finish/fail 的终态）。"""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [{"tag": "markdown", "content": text}],
    }
    return json.dumps(card, ensure_ascii=False)


def build_card_v2(text: str, title: str = TITLE_WORKING,
                  template: str = "blue") -> str:
    """schema 2.0 版静态卡片 content：占位是流式 schema 2.0 卡片时专用。

    飞书 PATCH 接口不允许 schema 2.0 的卡片被 schema 1.0 的 content 替换
    （实测 code=230099 "schemaV2 card can not change schemaV1"，
    2026-10-24）。此函数生成同等排版的 schema 2.0 静态卡片，
    保留原流式元素的 element_id 以便后续继续更新。
    """
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": text,
                 "element_id": STREAM_ELEMENT_ID}
            ]
        },
    }
    return json.dumps(card, ensure_ascii=False)


def build_stream_card(title: str = TITLE_WORKING) -> dict:
    """流式卡片实体：schema 2.0 + streaming_mode，body 里一个空 markdown 元素。"""
    freq = {"default": PRINT_FREQUENCY_MS, "android": PRINT_FREQUENCY_MS,
            "ios": PRINT_FREQUENCY_MS, "pc": PRINT_FREQUENCY_MS}
    step = {"default": 1, "android": 1, "ios": 1, "pc": 1}
    return {
        "schema": "2.0",
        "header": {"title": {"tag": "plain_text", "content": title}},
        "config": {
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": freq,
                "print_step": step,
                "print_strategy": "fast",
            },
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": "",
                 "element_id": STREAM_ELEMENT_ID}
            ]
        },
    }


def _headers() -> dict:
    cred = load_credentials()
    token = get_tenant_access_token(cred["app_id"], cred["app_secret"])
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


# ---------------------------------------------------------------------------
# 流式卡片状态（sidecar 文件，按 queue_id 存 card_id/sequence/是否降级）
# ---------------------------------------------------------------------------

def _state_path() -> pathlib.Path:
    return store.data_dir() / "streaming_state.json"


def _load_states() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_states(states: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(states, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)  # 原子写入，避免并发 worker 写坏


def _get_state(queue_id: int) -> dict | None:
    return _load_states().get(str(queue_id))


def _set_state(queue_id: int, state: dict | None) -> None:
    states = _load_states()
    if state is None:
        states.pop(str(queue_id), None)
    else:
        states[str(queue_id)] = state
    _save_states(states)


# ---------------------------------------------------------------------------
# CardKit 流式接口（requests 直调，不依赖 lark-oapi 版本）
# ---------------------------------------------------------------------------

def create_stream_card(title: str = TITLE_WORKING) -> str | None:
    """创建流式卡片实体，返回 card_id。失败返回 None（缺权限时走降级）。"""
    try:
        resp = requests.post(
            CARDKIT_CREATE_URL,
            json={"type": "card_json",
                  "data": json.dumps(build_stream_card(title),
                                     ensure_ascii=False)},
            headers=_headers(),
            timeout=10,
        )
        payload = resp.json()
    except Exception as e:
        print(f"[progress] 创建流式卡片异常: {e}", file=sys.stderr)
        return None
    if payload.get("code") != 0:
        print(f"[progress] 创建流式卡片失败（将降级为整卡 PATCH）: "
              f"code={payload.get('code')} msg={payload.get('msg')}",
              file=sys.stderr)
        return None
    card_id = payload.get("data", {}).get("card_id")
    if not card_id:
        print(f"[progress] 创建流式卡片响应无 card_id: {payload}",
              file=sys.stderr)
    return card_id


def push_stream_text(card_id: str, content: str, sequence: int,
                     element_id: str = STREAM_ELEMENT_ID) -> bool:
    """把累计全文推送到卡片元素，sequence 必须严格递增。"""
    try:
        resp = requests.put(
            CARDKIT_ELEMENT_URL.format(card_id=card_id, element_id=element_id),
            json={"uuid": str(uuid.uuid4()), "content": content,
                  "sequence": sequence},
            headers=_headers(),
            timeout=10,
        )
        payload = resp.json()
    except Exception as e:
        print(f"[progress] 流式推送异常: {e}", file=sys.stderr)
        return False
    if payload.get("code") != 0:
        print(f"[progress] 流式推送失败（将降级为整卡 PATCH）: "
              f"code={payload.get('code')} msg={payload.get('msg')}",
              file=sys.stderr)
        return False
    return True


def send_card_reference(chat_id: str, reply_to: str, card_id: str) -> str | None:
    """以回复形式发送一张已创建的卡片实体（interactive 引用 card_id）。"""
    try:
        resp = requests.post(
            REPLY_URL.format(message_id=reply_to),
            json={"msg_type": "interactive",
                  "content": json.dumps(
                      {"type": "card", "data": {"card_id": card_id}},
                      ensure_ascii=False),
                  "chat_id": chat_id},
            headers=_headers(),
            timeout=10,
        )
        payload = resp.json()
    except Exception as e:
        print(f"[progress] 发送卡片引用失败: {e}", file=sys.stderr)
        return None
    if payload.get("code") != 0:
        print(f"[progress] 发送卡片引用失败: code={payload.get('code')} "
              f"msg={payload.get('msg')}", file=sys.stderr)
        return None
    return payload.get("data", {}).get("message_id")


def add_reaction(message_id: str, emoji_type: str = "OnIt") -> bool:
    """给用户原消息加一个状态反应（尽力而为，失败不影响主流程）。"""
    try:
        resp = requests.post(
            REACTION_URL.format(message_id=message_id),
            json={"reaction_type": {"emoji_type": emoji_type}},
            headers=_headers(),
            timeout=10,
        )
        return resp.json().get("code") == 0
    except Exception:
        return False


def send_placeholder(
    chat_id: str,
    reply_to: str,
    queue_id: int | None = None,
    feishu_message_id: str | None = None,
    text: str = DEFAULT_PLACEHOLDER,
) -> str | None:
    """发占位回执（回复用户原消息），并把回执 id 存回队列行。返回回执 message_id。

    优先走流式卡片：建卡 → 发卡片引用 → 推送占位文本（seq=1，打字机开场）。
    任一步失败则整体降级为旧的整卡 interactive 回执。
    """
    if queue_id is None and feishu_message_id:
        row = store.find_by_feishu_id(feishu_message_id)
        if row:
            queue_id = row["id"]

    # --- 流式路径 ---
    card_id = create_stream_card(title="⏳ 已收到")
    if card_id:
        mid = send_card_reference(chat_id, reply_to, card_id)
        if mid and push_stream_text(card_id, text, sequence=1):
            if queue_id is not None:
                store.set_progress_message_id(queue_id, mid)
                _set_state(queue_id, {"card_id": card_id,
                                      "element_id": STREAM_ELEMENT_ID,
                                      "sequence": 1, "streaming": True})
            add_reaction(reply_to)
            return mid
        print("[progress] 流式占位未建成完整链路，降级为整卡回执",
              file=sys.stderr)

    # --- 降级路径：旧的整卡 interactive 回执 ---
    try:
        resp = requests.post(
            REPLY_URL.format(message_id=reply_to),
            json={"msg_type": "interactive",
                  "content": build_card(text, title="⏳ 已收到")},
            headers=_headers(),
            timeout=10,
        )
        payload = resp.json()
    except Exception as e:
        print(f"[progress] 占位回执发送失败: {e}", file=sys.stderr)
        return None
    if payload.get("code") != 0:
        print(f"[progress] 占位回执发送失败: code={payload.get('code')} "
              f"msg={payload.get('msg')}", file=sys.stderr)
        return None
    mid = payload.get("data", {}).get("message_id")
    if mid and queue_id is not None:
        store.set_progress_message_id(queue_id, mid)
        _set_state(queue_id, {"streaming": False})  # 记一笔：本轮只用 PATCH
    add_reaction(reply_to)
    return mid


def update_progress(queue_id: int, text: str,
                    title: str = TITLE_WORKING, template: str = "blue") -> bool:
    """把队列行的回执更新为 text：流式可用时打字机推送，否则整卡 PATCH。"""
    row = store.get_message(queue_id)
    if not row or not row.get("progress_message_id"):
        print(f"[progress] 队列 {queue_id} 没有回执消息，无法更新进度",
              file=sys.stderr)
        return False

    state = _get_state(queue_id)
    if state and state.get("streaming") and state.get("card_id"):
        seq = int(state.get("sequence", 0)) + 1
        if push_stream_text(state["card_id"], text, seq,
                            state.get("element_id", STREAM_ELEMENT_ID)):
            state["sequence"] = seq
            _set_state(queue_id, state)
            return True
        # 推送失败：降级为整卡 PATCH，后续更新不再尝试流式
        state["streaming"] = False
        _set_state(queue_id, state)

    # 流式卡片建的占位是 schema 2.0，PATCH 必须用 schema 2.0 content；
    # 纯降级占位（整卡 interactive）用旧 schema 1.0 content。
    schema_v2 = bool(state and state.get("card_id"))
    return patch_message(row["progress_message_id"], text,
                         title=title, template=template,
                         schema_v2=schema_v2)


def patch_message(message_id: str, text: str,
                  title: str = TITLE_WORKING, template: str = "blue",
                  schema_v2: bool = False) -> bool:
    content = (build_card_v2(text, title=title, template=template)
               if schema_v2 else build_card(text, title=title,
                                            template=template))
    try:
        resp = requests.patch(
            UPDATE_URL.format(message_id=message_id),
            json={"msg_type": "interactive",
                  "content": content},
            headers=_headers(),
            timeout=10,
        )
        payload = resp.json()
    except Exception as e:
        print(f"[progress] 更新回执失败: {e}", file=sys.stderr)
        return False
    if payload.get("code") != 0:
        print(f"[progress] 更新回执失败: code={payload.get('code')} "
              f"msg={payload.get('msg')}", file=sys.stderr)
        return False
    return True


def _final_stream_push(queue_id: int, text: str) -> None:
    """finish/fail 前：把最终全文再推一次流式（打字机收尾），失败就跳过。"""
    state = _get_state(queue_id)
    if state and state.get("streaming") and state.get("card_id"):
        seq = int(state.get("sequence", 0)) + 1
        if push_stream_text(state["card_id"], text, seq,
                            state.get("element_id", STREAM_ELEMENT_ID)):
            state["sequence"] = seq
            state["streaming"] = False  # 终态走静态卡，不再流式
            _set_state(queue_id, state)


def finish(queue_id: int, text: str) -> bool:
    """回执替换为最终答复（流式收尾 + 静态绿卡定稿），并 mark_done。"""
    _final_stream_push(queue_id, text)
    ok = update_progress(queue_id, text, title=TITLE_DONE, template="green")
    if ok:
        store.mark_done(queue_id, text)
        _set_state(queue_id, None)  # 清理流式状态
    return ok


def fail(queue_id: int, text: str, error: str) -> bool:
    """回执替换为失败说明（静态红卡定稿），并 mark_failed。"""
    _final_stream_push(queue_id, text)
    ok = update_progress(queue_id, text, title=TITLE_FAILED, template="red")
    store.mark_failed(queue_id, error)
    _set_state(queue_id, None)  # 清理流式状态
    return ok


def delete_message(message_id: str) -> bool:
    """删除机器人自己发的一条消息（清理废弃回执用，尽力而为）。"""
    try:
        resp = requests.delete(
            UPDATE_URL.format(message_id=message_id),
            headers=_headers(),
            timeout=10,
        )
        return resp.json().get("code") == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="飞书桥接 - 进度回执")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="发送占位回执")
    p_start.add_argument("--chat-id", required=True)
    p_start.add_argument("--reply-to", required=True, help="用户原消息 message_id")
    p_start.add_argument("--queue-id", type=int)
    p_start.add_argument("--feishu-message-id")
    p_start.add_argument("--text", default=DEFAULT_PLACEHOLDER)

    p_upd = sub.add_parser("update", help="原地更新回执进度")
    p_upd.add_argument("--queue-id", type=int, required=True)
    p_upd.add_argument("--text", required=True)

    p_fin = sub.add_parser("finish", help="回执替换为最终答复并 mark_done")
    p_fin.add_argument("--queue-id", type=int, required=True)
    p_fin.add_argument("--text", required=True)

    p_fail = sub.add_parser("fail", help="回执替换为失败说明并 mark_failed")
    p_fail.add_argument("--queue-id", type=int, required=True)
    p_fail.add_argument("--text", required=True)
    p_fail.add_argument("--error", required=True)

    args = parser.parse_args()

    if args.cmd == "start":
        mid = send_placeholder(
            args.chat_id,
            args.reply_to,
            queue_id=args.queue_id,
            feishu_message_id=args.feishu_message_id,
            text=args.text,
        )
        if not mid:
            return 1
        print(f"[ok] 回执已发送: message_id={mid}")
        return 0
    if args.cmd == "update":
        return 0 if update_progress(args.queue_id, args.text) else 1
    if args.cmd == "finish":
        return 0 if finish(args.queue_id, args.text) else 1
    if args.cmd == "fail":
        return 0 if fail(args.queue_id, args.text) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
