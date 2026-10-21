"""飞书长连接接入：通过 lark-oapi WebSocket 接收 im.message.receive_v1 事件并入队。

依赖 lark-oapi（见 requirements.txt）。长连接断开由 SDK 自动重连。
"""

import json
import os
import pathlib
import re
import sys

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

# --- 代理补丁（适用于只能经 HTTP 代理出网的环境）---
# 若运行环境直连外网不通、必须走 HTTP 代理：lark-oapi 1.7.5 的
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

    msg = {
        "feishu_message_id": getattr(message, "message_id", None),
        "chat_id": getattr(message, "chat_id", None),
        "chat_type": getattr(message, "chat_type", None),
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
    if msg["text"] is None:
        # 解析兜底日志：正文没提取出来时把原始 content 打进日志，避免内容静默丢失
        raw = getattr(message, "content", None)
        print(f"[ingest][warn] text 解析为空，原始 content={str(raw)[:500]}", flush=True)


def main() -> None:
    cred = load_credentials()

    dispatcher = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .build()
    )

    ws_client = lark.ws.Client(
        app_id=cred["app_id"],
        app_secret=cred["app_secret"],
        event_handler=dispatcher,
        log_level=lark.LogLevel.INFO,
    )
    print(f"[ingest] 开始连接飞书长连接 (app_id={cred['app_id'][:8]}...)")
    ws_client.start()  # 阻塞运行；断线由 SDK 自动重连


if __name__ == "__main__":
    main()
