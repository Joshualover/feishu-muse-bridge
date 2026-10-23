"""飞书桥接进度回执：消息一到先发占位回执，处理中原地更新，最后变成正式答复。

解决两个体验问题：
1. 延迟感：占位回执在消息入队后立刻发出（1~2 秒），用户能立刻确认桥是活的；
2. 黑盒感：处理进程把每一步进度 PATCH 到同一条回执上，最后一步替换为正式
   答复全文，用户全程看得见在干什么，不用猜是卡住还是断线。

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
import sys

import requests

import bridge.queue_store as store
from bridge.send_reply import get_tenant_access_token, load_credentials

REPLY_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
UPDATE_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
REACTION_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions"

DEFAULT_PLACEHOLDER = (
    "⏳ **已收到**，Edison 正在处理…\n"
    "（这张卡片会实时更新进度，最后变成正式答复）"
)

TITLE_WORKING = "🔄 Edison 正在处理"
TITLE_DONE = "✅ 处理完成"
TITLE_FAILED = "⚠️ 处理失败"


def build_card(text: str, title: str = TITLE_WORKING, template: str = "blue") -> str:
    """进度卡片 content：卡片是飞书唯一支持原地编辑（PATCH）的消息类型，
    且其 markdown 元素能真实渲染 **加粗**。post/text 消息无法被编辑，
    2026-10-22 实测 PATCH 返回 230001 "This message is NOT a card"。"""
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [{"tag": "markdown", "content": text}],
    }
    return json.dumps(card, ensure_ascii=False)


def _headers() -> dict:
    cred = load_credentials()
    token = get_tenant_access_token(cred["app_id"], cred["app_secret"])
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


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
    """发占位回执（回复用户原消息），并把回执 id 存回队列行。返回回执 message_id。"""
    if queue_id is None and feishu_message_id:
        row = store.find_by_feishu_id(feishu_message_id)
        if row:
            queue_id = row["id"]
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
    add_reaction(reply_to)
    return mid


def update_progress(queue_id: int, text: str,
                    title: str = TITLE_WORKING, template: str = "blue") -> bool:
    """把队列行的回执卡片原地更新为 text。"""
    row = store.get_message(queue_id)
    if not row or not row.get("progress_message_id"):
        print(f"[progress] 队列 {queue_id} 没有回执消息，无法更新进度",
              file=sys.stderr)
        return False
    return patch_message(row["progress_message_id"], text,
                         title=title, template=template)


def patch_message(message_id: str, text: str,
                  title: str = TITLE_WORKING, template: str = "blue") -> bool:
    try:
        resp = requests.patch(
            UPDATE_URL.format(message_id=message_id),
            json={"msg_type": "interactive",
                  "content": build_card(text, title=title, template=template)},
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


def finish(queue_id: int, text: str) -> bool:
    """回执替换为最终答复，并 mark_done。"""
    ok = update_progress(queue_id, text, title=TITLE_DONE, template="green")
    if ok:
        store.mark_done(queue_id, text)
    return ok


def fail(queue_id: int, text: str, error: str) -> bool:
    """回执替换为失败说明，并 mark_failed。"""
    ok = update_progress(queue_id, text, title=TITLE_FAILED, template="red")
    store.mark_failed(queue_id, error)
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
        return 0 if fail(args.queue_id, args.text, args.error) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
