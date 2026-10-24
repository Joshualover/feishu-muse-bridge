"""SQLite 消息队列：飞书收到的消息先落库，再由外部 agent 轮询处理。"""

import json
import os
import pathlib
import sqlite3
import sys

DB_FILE_NAME = "queue.db"
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feishu_message_id TEXT UNIQUE,
    chat_id TEXT,
    chat_type TEXT,
    sender_open_id TEXT,
    msg_type TEXT,
    text TEXT,
    create_time_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    claimed_at TEXT,
    progress_message_id TEXT,
    reply_text TEXT,
    error TEXT,
    received_at TEXT,
    replied_at TEXT
);
"""


def data_dir() -> pathlib.Path:
    """队列数据目录：优先取环境变量 FEISHU_BRIDGE_DATA_DIR，默认 ~/feishu-bridge-data。"""
    raw = os.environ.get("FEISHU_BRIDGE_DATA_DIR")
    if raw:
        return pathlib.Path(raw).expanduser()
    return pathlib.Path.home() / "feishu-bridge-data"


def db_path() -> pathlib.Path:
    return data_dir() / DB_FILE_NAME


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute(_SCHEMA)
        # 迁移：旧库补新列（claimed_at=处理开始时间用于卡死自愈；
        # progress_message_id=进度回执消息 id，供原地更新进度）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        for col in ("claimed_at", "progress_message_id"):
            if col not in cols:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        conn.commit()
    finally:
        conn.close()


def enqueue(msg: dict) -> bool:
    """入队一条消息；按 feishu_message_id 去重，重复时返回 False。"""
    feishu_message_id = msg.get("feishu_message_id")
    if not feishu_message_id:
        raise ValueError("enqueue: feishu_message_id 不能为空")

    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO messages
                (feishu_message_id, chat_id, chat_type, sender_open_id, msg_type,
                 text, create_time_ms, status, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
            """,
            (
                feishu_message_id,
                msg.get("chat_id"),
                msg.get("chat_type"),
                msg.get("sender_open_id"),
                msg.get("msg_type"),
                msg.get("text"),
                msg.get("create_time_ms"),
                STATUS_PENDING,
                msg.get("received_at"),
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_pending(limit: int = 10) -> list[dict]:
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM messages
            WHERE status = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (STATUS_PENDING, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_processing(id: int) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE messages SET status = ?, claimed_at = datetime('now') "
            "WHERE id = ? AND status = ?",
            (STATUS_PROCESSING, id, STATUS_PENDING),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_done(id: int, reply_text: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            """
            UPDATE messages SET status = ?, reply_text = ?,
                replied_at = datetime('now'), claimed_at = NULL
            WHERE id = ?
            """,
            (STATUS_DONE, reply_text, id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def mark_failed(id: int, error: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE messages SET status = ?, error = ?, claimed_at = NULL "
            "WHERE id = ?",
            (STATUS_FAILED, error, id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_message(id: int) -> dict | None:
    init_db()
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_by_feishu_id(feishu_message_id: str) -> dict | None:
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM messages WHERE feishu_message_id = ?",
            (feishu_message_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_progress_message_id(id: int, progress_message_id: str) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE messages SET progress_message_id = ? WHERE id = ?",
            (progress_message_id, id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reset_stale_processing(minutes: int = 10) -> int:
    """把卡在 processing 超过 minutes 分钟的消息回滚为 pending，返回回滚条数。

    处理进程中途死掉时消息会永远停在 processing、无人再管；靠这个函数自愈。
    claimed_at 为空（旧数据）时用 received_at 兜底判断。
    """
    init_db()
    conn = _connect()
    try:
        cur = conn.execute(
            """
            UPDATE messages SET status = ?, claimed_at = NULL
            WHERE status = ?
              AND COALESCE(claimed_at, received_at)
                  < datetime('now', '-' || ? || ' minutes')
            """,
            (STATUS_PENDING, STATUS_PROCESSING, int(minutes)),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def stats() -> dict:
    init_db()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM messages GROUP BY status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        by_status = {r["status"]: r["n"] for r in rows}
        return {"total": total, **by_status}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI: python3 -m bridge.queue_store add / pending / stats
# ---------------------------------------------------------------------------

_USAGE = """用法:
  python3 -m bridge.queue_store add --json '<消息JSON>'
  python3 -m bridge.queue_store pending [--limit N]
  python3 -m bridge.queue_store stats
  python3 -m bridge.queue_store reset-stale [--minutes N]
"""


def _cli_add(argv: list[str]) -> int:
    if argv and argv[0] == "--json" and len(argv) > 1:
        try:
            msg = json.loads(argv[1])
        except json.JSONDecodeError as e:
            print(f"JSON 解析失败: {e}", file=sys.stderr)
            return 2
        ok = enqueue(msg)
        print(f"入队成功: {msg.get('feishu_message_id')}" if ok
              else f"已存在(去重跳过): {msg.get('feishu_message_id')}")
        return 0
    print(_USAGE, file=sys.stderr)
    return 2


def _cli_pending(argv: list[str]) -> int:
    limit = 10
    if argv and argv[0] == "--limit" and len(argv) > 1:
        try:
            limit = int(argv[1])
        except ValueError:
            print(f"limit 参数非法: {argv[1]}", file=sys.stderr)
            return 2
    rows = list_pending(limit)
    if not rows:
        print("(无 pending 消息)")
        return 0
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def _cli_stats() -> int:
    s = stats()
    print(json.dumps(s, ensure_ascii=False))
    return 0


def _cli_reset_stale(argv: list[str]) -> int:
    minutes = 10
    if argv and argv[0] == "--minutes" and len(argv) > 1:
        try:
            minutes = int(argv[1])
        except ValueError:
            print(f"minutes 参数非法: {argv[1]}", file=sys.stderr)
            return 2
    n = reset_stale_processing(minutes)
    print(f"已回滚卡住的 processing 消息: {n} 条")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print(_USAGE, file=sys.stderr)
        return 2
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "add":
        return _cli_add(rest)
    if cmd == "pending":
        return _cli_pending(rest)
    if cmd == "stats":
        return _cli_stats()
    if cmd == "reset-stale":
        return _cli_reset_stale(rest)
    print(f"未知命令: {cmd}", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
