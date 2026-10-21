"""通过飞书 API 回发消息。

用法:
  python3 -m bridge.send_reply --chat-id <chat_id> --text "你好"
  python3 -m bridge.send_reply --chat-id <chat_id> --reply-to <message_id> --text "回复"
  python3 -m bridge.send_reply --chat-id <chat_id> --text "测试" --dry-run
"""

import argparse
import json
import os
import pathlib
import sys

import requests


def _data_dir() -> pathlib.Path:
    raw = os.environ.get("FEISHU_BRIDGE_DATA_DIR")
    if raw:
        return pathlib.Path(raw).expanduser()
    return pathlib.Path.home() / "feishu-bridge-data"


def load_credentials() -> dict:
    """从数据目录 credentials.json 读取 app_id / app_secret（仅标准库实现，
    不依赖 ingest/lark_oapi，保证 --dry-run 在任何环境可跑）。"""
    path = _data_dir() / "credentials.json"
    if not path.exists():
        print(f"[错误] 找不到凭证文件: {path}", file=sys.stderr)
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

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
REPLY_URL = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    resp = requests.post(
        TOKEN_URL,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(f"取 token 响应不是 JSON: HTTP {resp.status_code} {resp.text[:200]}")
    if payload.get("code") != 0:
        raise RuntimeError(
            f"取 token 失败: code={payload.get('code')} msg={payload.get('msg')}"
        )
    token = payload.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"取 token 失败: 响应中无 tenant_access_token: {payload}")
    return token


def build_content(text: str) -> str:
    """text 消息 content 是 JSON 字符串 {\"text\": ...}。"""
    return json.dumps({"text": text}, ensure_ascii=False)


def send_via_api(args) -> dict:
    cred = load_credentials()
    token = get_tenant_access_token(cred["app_id"], cred["app_secret"])
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    content = build_content(args.text)

    if args.reply_to:
        url = REPLY_URL.format(message_id=args.reply_to)
        body = {"msg_type": "text", "content": content}
        if args.chat_id:
            body["chat_id"] = args.chat_id
    else:
        url = SEND_URL
        body = {
            "receive_id": args.chat_id,
            "msg_type": "text",
            "content": content,
        }

    resp = requests.post(url, json=body, headers=headers, timeout=10)
    try:
        payload = resp.json()
    except ValueError:
        raise RuntimeError(
            f"发送响应不是 JSON: HTTP {resp.status_code} {resp.text[:200]}"
        )
    return {"http_status": resp.status_code, "payload": payload}


def dry_run_payload(args) -> dict:
    if args.reply_to:
        return {
            "url": REPLY_URL.format(message_id=args.reply_to),
            "headers": {"Authorization": "Bearer <tenant_access_token>"},
            "body": {
                "msg_type": "text",
                "content": build_content(args.text),
                **({"chat_id": args.chat_id} if args.chat_id else {}),
            },
        }
    return {
        "url": SEND_URL,
        "headers": {"Authorization": "Bearer <tenant_access_token>"},
        "body": {
            "receive_id": args.chat_id,
            "msg_type": "text",
            "content": build_content(args.text),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="飞书桥接 - 消息回发")
    parser.add_argument("--chat-id", required=True, help="chat_id（p2p 或群聊）")
    parser.add_argument("--text", required=True, help="要发送的文本内容")
    parser.add_argument("--reply-to", help="可选：要回复的飞书 message_id")
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印将发送的 payload，不联网"
    )
    args = parser.parse_args()

    if args.dry_run:
        # 干跑不联网、不需要凭证
        print(json.dumps(dry_run_payload(args), ensure_ascii=False, indent=2))
        return 0
    try:
        result = send_via_api(args)
    except SystemExit as e:
        return int(e.code or 1)
    except (requests.RequestException, RuntimeError) as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1

    payload = result["payload"]
    if payload.get("code") != 0:
        print(
            f"[错误] 发送失败: code={payload.get('code')} msg={payload.get('msg')} "
            f"HTTP {result['http_status']}",
            file=sys.stderr,
        )
        return 1

    msg = payload.get("data", {})
    print(
        f"[ok] 发送成功: message_id={msg.get('message_id')} "
        f"chat_id={msg.get('chat_id')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
