#!/usr/bin/env python3
"""示例消费者：轮询桥接队列，把消息交给任意 OpenAI 兼容的聊天接口生成回复，再回发到飞书。

用法:
  export LLM_BASE_URL="https://api.deepseek.com/v1"   # 任意 OpenAI 兼容 endpoint
  export LLM_API_KEY="<你的-deepseek-key>"
  export LLM_MODEL="deepseek-chat"
  python3 examples/consumer_openai.py            # 常驻轮询（5 秒间隔）
  python3 examples/consumer_openai.py --once     # 只处理一轮就退出

说明:
  - 这只是一个最小示例，演示 queue_store 的消费方式（list_pending →
    mark_processing → 处理 → send_reply → mark_done / mark_failed）。
  - 把它换成你自己的 agent / 工作流即可：队列就是桥和处理器之间的全部接口。
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

import bridge.queue_store as store


def generate_reply(text: str) -> str:
    base = os.environ["LLM_BASE_URL"].rstrip("/")
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
        json={
            "model": os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": "你是一个通过飞书对话的助手，回答简洁、准确。"},
                {"role": "user", "content": text},
            ],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def send_reply(row: dict, reply_text: str) -> None:
    root = os.path.join(os.path.dirname(__file__), "..")
    subprocess.run(
        [
            sys.executable, "-m", "bridge.send_reply",
            "--chat-id", row["chat_id"],
            "--reply-to", row["feishu_message_id"],
            "--text", reply_text,
        ],
        cwd=root, check=True, capture_output=True, text=True,
    )


def process_pending() -> int:
    rows = store.list_pending(limit=10)
    for row in rows:
        store.mark_processing(row["id"])
        try:
            if not row.get("text"):
                raise ValueError("消息正文为空（非文本消息或解析失败）")
            reply = generate_reply(row["text"])
            send_reply(row, reply)
            store.mark_done(row["id"], reply)
            print(f"[consumer] 已回复 message_id={row['feishu_message_id']}")
        except Exception as e:  # 单条失败不影响其他消息
            store.mark_failed(row["id"], str(e))
            print(f"[consumer] 处理失败 id={row['id']}: {e}", file=sys.stderr)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书桥接示例消费者（OpenAI 兼容 LLM）")
    parser.add_argument("--once", action="store_true", help="只处理一轮")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒数")
    args = parser.parse_args()

    if args.once:
        process_pending()
        return
    print("[consumer] 开始轮询队列，Ctrl+C 退出")
    while True:
        process_pending()
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
