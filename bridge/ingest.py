"""飞书长连接接入：通过 lark-oapi WebSocket 接收 im.message.receive_v1 事件并入队。

依赖 lark-oapi（见 requirements.txt）。长连接断开由 SDK 自动重连。
"""

import json
import os
import pathlib
import re
import sys
import threading
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

# --- 代理补丁（本机环境专用）---
# 本 VM 直连外网不通，必须走环境变量里的 HTTP 代理；而 lark-oapi 1.7.5 的
# ws 客户端写死了 proxy=None（直连），导致 TLS 握手 WRONG_VERSION_NUMBER。
# 这里包装 websockets.connect：SDK 传 proxy=None 时，改填环境代理，走 CONNECT 隧道。
import websockets as _websockets

_orig_ws_connect = _websockets.connect


def _proxied_ws_connect(*args, **kwargs):
    if kwargs.get("proxy") is None:
        proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("all_proxy")
            or os.environ.get("ALL_PROXY")
        )
        if proxy:
            kwargs["proxy"] = proxy
    return _orig_ws_connect(*args, **kwargs)


_websockets.connect = _proxied_ws_connect
# --- 代理补丁结束 ---

# 匹配 @ 提及占位符，例如 @_user_1、@_all（后面可能带空格）
_MENTION_PLACEHOLDER = re.compile(r"@_user_\d+|@_all")
# 匹配事件中附带 mention 的 open_id 占位形式（防御性清理）
_MENTION_OPENID = re.compile(r"@ou_[A-Za-z0-9]+")


def data_dir() -> pathlib.Path:
    raw = os.environ.get("FEISHU_BRIDGE_DATA_DIR")
    if raw:
        return pathlib.Path(raw).expanduser()
    return pathlib.Path.home() / "feishu-bridge-data"


CREDENTIALS_FILE = "credentials.json"


def load_credentials() -> dict:
    """从 ~/feishu-bridge-data/credentials.json 读取 app_id / app_secret。"""
    path = data_dir() / CREDENTIALS_FILE
    if not path.exists():
        print(f"[错误] 找不到凭证文件: {path}", file=sys.stderr)
        print("请参考 SETUP.md 将飞书应用凭证写入该文件（JSON: {\"app_id\": ..., \"app_secret\": ...}）。",
              file=sys.stderr)
        raise SystemExit(2)
    try:
        cred = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[错误] 凭证文件 JSON 解析失败: {path}: {e}", file=sys.stderr)
        raise SystemExit(2)
    if not cred.get("app_id") or not cred.get("app_secret"):
        print(f"[错误] 凭证文件 {path} 缺少 app_id 或 app_secret 字段。", file=sys.stderr)
        raise SystemExit(2)
    return cred


def extract_text(message) -> str | None:
    """从事件 message 中提取文本内容，返回纯文本（已去掉 @ 提及占位符）。

    注意 lark-oapi 1.7.x 的 EventMessage 字段是 message_type（不是 msg_type），
    正文是 message.content（JSON 字符串），没有 body 层。2026-10-21 首测时
    因字段名不符导致真实消息 text 全部丢失，靠 API 按 message_id 找回；
    此处两个字段名都兼容，并保留 body.content 兜底。
    """
    msg_type = getattr(message, "message_type", None) or getattr(message, "msg_type", None)
    if msg_type != "text":
        return None
    content = getattr(message, "content", None)
    if not content:
        body = getattr(message, "body", None)
        if body is not None:
            content = getattr(body, "content", None)
    if not content:
        return None
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    text = payload.get("text", "") if isinstance(payload, dict) else ""
    text = _MENTION_PLACEHOLDER.sub("", text)
    text = _MENTION_OPENID.sub("", text)
    return text.strip()


def _send_auto_ack_text(chat_id: str, reply_to: str, timeout_s: int) -> None:
    """发送超时自动 ack 提醒：post 富文本，回复用户原消息。

    抛异常由调用方捕获并记日志，不中断长连接。
    """
    import argparse

    from bridge.send_reply import send_via_api

    text = (
        f"⚠️ **已收到你的消息**，但 {timeout_s} 秒后还没开始处理——"
        "处理进程可能没起来或卡住了。\n"
        "这是自动兜底提醒，你可以稍等一下；如果长时间没动静，"
        "可以直接再发一次消息。"
    )
    args = argparse.Namespace(
        chat_id=chat_id, reply_to=reply_to, text=text, format="post"
    )
    result = send_via_api(args)
    payload = result["payload"]
    if payload.get("code") != 0:
        raise RuntimeError(
            f"自动 ack 发送失败: code={payload.get('code')} msg={payload.get('msg')}"
        )


def _watch_auto_ack(queue_id: int, feishu_message_id: str, chat_id: str,
                    timeout_s: int) -> None:
    """自动 ack 监视线程：睡眠 timeout_s 后检查消息是否仍为 pending。

    只有钩子/worker 故障、消息迟迟无人认领时才真正发提醒；
    正常情况（几秒内被认领）直接返回，静默无扰。
    """
    time.sleep(timeout_s)
    try:
        import bridge.queue_store as store

        row = store.get_message(queue_id)
    except Exception as e:
        print(f"[ingest][autoack] 查询队列 {queue_id} 失败: {e}", flush=True)
        return
    if row is None or row.get("status") != "pending":
        return  # 已被认领或处理完，无需提醒
    try:
        _send_auto_ack_text(chat_id, feishu_message_id, timeout_s)
        print(f"[ingest][autoack] 队列 {queue_id} {timeout_s}s 未认领，已发兜底提醒",
              flush=True)
    except Exception as e:
        print(f"[ingest][autoack] 兜底提醒发送失败: {e}", flush=True)


def handle_message(data: P2ImMessageReceiveV1) -> None:
    """im.message.receive_v1 事件处理器。"""
    import bridge.queue_store as store

    event = getattr(data, "event", None)
    if event is None:
        print("[ingest] 事件缺少 event 字段，跳过")
        return
    message = getattr(event, "message", None)
    if message is None:
        print("[ingest] 事件缺少 message 字段，跳过")
        return

    sender = getattr(event, "sender", None)
    sender_id = None
    if sender is not None:
        sid = getattr(sender, "sender_id", None)
        if sid is not None:
            sender_id = getattr(sid, "open_id", None)

    chat_type = getattr(message, "chat_type", None)

    # 发送者白名单：陌生人消息静默忽略（不入库、不回执），
    # 防止陌生人通过飞书私聊/拉群指挥 Edison（借鉴 muse2feishu 的
    # allowed_sender_open_ids，2026-10-23 用户确认执行）。
    import bridge.config as config

    if not config.is_sender_allowed(sender_id, chat_type):
        print(f"[ingest] 白名单拦截: sender={sender_id} chat_type={chat_type}"
              f"（静默忽略，不入库）", flush=True)
        return

    msg = {
        "feishu_message_id": getattr(message, "message_id", None),
        "chat_id": getattr(message, "chat_id", None),
        "chat_type": chat_type,
        "sender_open_id": sender_id,
        "msg_type": getattr(message, "message_type", None) or getattr(message, "msg_type", None),
        "text": extract_text(message),
        "create_time_ms": int(getattr(message, "create_time", "0") or 0),
        "received_at": None,  # 由 queue_store 记录；这里可留空
    }

    try:
        ok = store.enqueue(msg)
    except Exception as e:  # 入队失败不应导致长连接中断
        print(f"[ingest] 入队失败: {e}")
        return

    status = "已入队" if ok else "重复消息(跳过)"
    print(f"[ingest] receive message_id={msg['feishu_message_id']} "
          f"chat_type={msg['chat_type']} msg_type={msg['msg_type']} → {status}", flush=True)

    if ok:
        # 即时回执：立刻发一条占位消息，处理进程会把进度原地更新到这条上、
        # 最后替换为正式答复；让用户能立刻确认桥是活的，不用猜卡住还是断线。
        try:
            import bridge.progress as progress

            mid = progress.send_placeholder(
                msg["chat_id"],
                msg["feishu_message_id"],
                feishu_message_id=msg["feishu_message_id"],
            )
            print(f"[ingest] 进度回执: {mid}", flush=True)
        except Exception as e:  # 回执失败不影响已入队的消息
            print(f"[ingest] 进度回执发送失败（不影响入队）: {e}", flush=True)

        # 超时自动 ack：N 秒后消息仍无人认领（钩子/worker 没起来，见 10-22 卡死
        # 事故），自动发一条文字提醒兜底。正常情况几秒内就被认领，这条永远不触发。
        try:
            cfg = config.load_config()
            if cfg.get("auto_ack_enabled", True):
                row = store.find_by_feishu_id(msg["feishu_message_id"])
                if row is not None:
                    timeout_s = int(cfg.get("auto_ack_seconds", 60) or 60)
                    threading.Thread(
                        target=_watch_auto_ack,
                        args=(row["id"], msg["feishu_message_id"],
                              msg["chat_id"], timeout_s),
                        daemon=True,
                        name=f"autoack-{row['id']}",
                    ).start()
        except Exception as e:  # 定时器起不来不影响主流程
            print(f"[ingest] 自动 ack 定时器启动失败: {e}", flush=True)

    if msg["text"] is None:
        # 解析兜底日志：正文没提取出来时把原始 content 打进日志，避免内容静默丢失
        raw = getattr(message, "content", None)
        print(f"[ingest][warn] text 解析为空，原始 content={str(raw)[:500]}", flush=True)


HEARTBEAT_FILE = "ingest-heartbeat.json"


def _heartbeat_loop() -> None:
    """每 20 秒写一次心跳文件，供看门狗钩子判断 ingest 是否还活着。

    2026-10-22 教训：WS 断开后 SDK 偶发不重连、进程直接退出，桥聋了
    12 分钟用户才发现。心跳停更 >90 秒，看门狗就跑 healthcheck 拉起进程。
    """
    path = data_dir() / HEARTBEAT_FILE
    while True:
        try:
            path.write_text(json.dumps({"ts": time.time(), "pid": os.getpid()}))
        except Exception:
            pass
        time.sleep(20)


def _ignore_event(data) -> None:
    """反应类事件的空处理器：机器人自己加的 OnIt 反应会回流成事件，
    不注册处理器时 SDK 会打 ERROR 日志（processor not found），无害但扰民。"""
    return None


def _run_client(cred: dict) -> None:
    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .register_p2_im_message_reaction_created_v1(_ignore_event)
        .register_p2_im_message_reaction_deleted_v1(_ignore_event)
        .build()
    )

    ws_client = lark.ws.Client(
        app_id=cred["app_id"],
        app_secret=cred["app_secret"],
        event_handler=dispatcher,
        log_level=lark.LogLevel.INFO,
    )
    print(f"[ingest] 开始连接飞书长连接 (app_id={cred['app_id'][:8]}...)", flush=True)
    ws_client.start()  # 阻塞运行；一般断线由 SDK 自动重连


def main() -> None:
    cred = load_credentials()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    # 进程级自愈：SDK 若放弃重连导致 start() 返回/抛错，5 秒后整客户端重启，
    # 不依赖外部巡检（2026-10-22 曾因此断线 12 分钟且期间消息丢失）。
    while True:
        try:
            _run_client(cred)
            print("[ingest] 长连接客户端已退出，5 秒后重启", flush=True)
        except Exception as e:
            print(f"[ingest] 长连接异常退出: {e}，5 秒后重启", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
