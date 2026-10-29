"""飞书桥接配置：白名单 + 超时自动 ack 参数。

配置存放在数据目录的 config.json（默认 ~/feishu-bridge-data/config.json，
可通过环境变量 FEISHU_BRIDGE_DATA_DIR 切换）：

{
  "allowed_sender_open_ids": ["ou_xxx"],   // 发送者白名单（open_id 列表）
  "whitelist_enabled": true,               // true 时只处理白名单内发送者
  "auto_ack_enabled": true,                // 超时自动 ack 开关
  "auto_ack_seconds": 60                   // 入队 N 秒仍无人认领则自动提醒
}

- 白名单：p2p 与群聊都只处理白名单内发送者的消息，陌生人消息**静默忽略**
  （不入库、不发回执、不回应），防止陌生人通过飞书指挥 Edison。
  白名单为空且开启时，全部消息都会被拦截（安全默认值），日志里会有提示。
- 超时自动 ack：正常情况消息入队后几秒内就会被认领，自动 ack 永远不会触发；
  只有处理进程长时间没起来（钩子/worker 故障）时才会发一条文字提醒兜底。

用法:
  python3 -m bridge.config show
  python3 -m bridge.config whitelist add <open_id>
  python3 -m bridge.config whitelist remove <open_id>
  python3 -m bridge.config whitelist on|off
  python3 -m bridge.config set auto_ack_seconds 60
"""

import json
import os
import pathlib
import sys

CONFIG_FILE = "config.json"

DEFAULTS = {
    "allowed_sender_open_ids": [],
    "whitelist_enabled": True,
    "auto_ack_enabled": True,
    "auto_ack_seconds": 60,
}


def config_path() -> pathlib.Path:
    raw = os.environ.get("FEISHU_BRIDGE_DATA_DIR")
    base = pathlib.Path(raw).expanduser() if raw else pathlib.Path.home() / "feishu-bridge-data"
    return base / CONFIG_FILE


def load_config() -> dict:
    """读配置；文件不存在时按 DEFAULTS 生成一份并返回。"""
    path = config_path()
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict):
                merged = dict(DEFAULTS)
                merged.update(cfg)
                return merged
        except json.JSONDecodeError as e:
            print(f"[config] 配置文件解析失败，使用默认值: {path}: {e}", file=sys.stderr)
            return dict(DEFAULTS)
    cfg = dict(DEFAULTS)
    save_config(cfg)
    print(f"[config] 已生成默认配置: {path}", file=sys.stderr)
    return cfg


def save_config(cfg: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # 原子写入


def is_sender_allowed(sender_open_id: str | None, chat_type: str | None = None) -> bool:
    """判断发送者是否允许触发处理。

    chat_type 保留参数位（p2p/群聊目前统一走白名单），返回 True 表示允许。
    """
    cfg = load_config()
    if not cfg.get("whitelist_enabled", True):
        return True
    allowed = cfg.get("allowed_sender_open_ids") or []
    if sender_open_id and sender_open_id in allowed:
        return True
    if not allowed:
        print(f"[config] 白名单为空（whitelist_enabled=true），"
              f"已拦截 sender={sender_open_id} chat_type={chat_type}。"
              f"请用 `python3 -m bridge.config whitelist add <open_id>` 加入自己的 open_id。",
              file=sys.stderr)
    return False


def whitelist_add(open_id: str) -> bool:
    cfg = load_config()
    allowed = cfg.get("allowed_sender_open_ids") or []
    if open_id in allowed:
        return False
    allowed.append(open_id)
    cfg["allowed_sender_open_ids"] = allowed
    save_config(cfg)
    return True


def whitelist_remove(open_id: str) -> bool:
    cfg = load_config()
    allowed = cfg.get("allowed_sender_open_ids") or []
    if open_id not in allowed:
        return False
    allowed.remove(open_id)
    cfg["allowed_sender_open_ids"] = allowed
    save_config(cfg)
    return True


_USAGE = """用法:
  python3 -m bridge.config show
  python3 -m bridge.config whitelist add <open_id>
  python3 -m bridge.config whitelist remove <open_id>
  python3 -m bridge.config whitelist on|off
  python3 -m bridge.config set <key> <value>
"""


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] == "show":
        print(json.dumps(load_config(), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "whitelist":
        if len(argv) == 3 and argv[1] == "add":
            print("已加入白名单" if whitelist_add(argv[2]) else "已在白名单中")
            return 0
        if len(argv) == 3 and argv[1] == "remove":
            print("已移出白名单" if whitelist_remove(argv[2]) else "不在白名单中")
            return 0
        if len(argv) == 2 and argv[1] in ("on", "off"):
            cfg = load_config()
            cfg["whitelist_enabled"] = argv[1] == "on"
            save_config(cfg)
            print(f"白名单已{'启用' if cfg['whitelist_enabled'] else '关闭'}")
            return 0
        print(_USAGE, file=sys.stderr)
        return 2
    if argv[0] == "set" and len(argv) == 3:
        key, raw = argv[1], argv[2]
        if raw.lower() in ("true", "false", "on", "off", "yes", "no"):
            value: object = raw.lower() in ("true", "on", "yes")
        else:
            try:
                value = int(raw)
            except ValueError:
                value = raw
        cfg = load_config()
        if key not in cfg:
            print(f"未知配置项: {key}（可选: {', '.join(cfg)}）", file=sys.stderr)
            return 2
        cfg[key] = value
        save_config(cfg)
        print(f"已设置 {key}={value}")
        return 0
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
