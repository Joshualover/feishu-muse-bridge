#!/usr/bin/env bash
# 启动飞书长连接接入进程（后台运行，日志与 pid 写入数据目录）。
# 判活必须校验进程命令行含 bridge.ingest：只看 pid 存活会被 pid 复用骗过
# （2026-10-23 / 2026-10-28 两次事故：ingest 死后 pid 被 Node/chrome-headless
# 进程回收复用，kill -0 误报存活，自动修复链被堵死）。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${FEISHU_BRIDGE_DATA_DIR:-$HOME/feishu-bridge-data}"
LOG_FILE="$DATA_DIR/ingest.log"
PID_FILE="$DATA_DIR/ingest.pid"

mkdir -p "$DATA_DIR"

cd "$PROJECT_DIR"

# 优先使用项目 .venv（内含 lark-oapi），否则退回系统 python3
PYTHON_BIN="python3"
if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
fi

# pid 文件里的进程是否真的是 bridge.ingest（防 pid 复用假阳性）。
# 注意：只删 pid 文件、绝不向该 pid 发信号——它可能是别人的进程。
ingest_alive() {
    local pid cmd
    [[ -f "$PID_FILE" ]] || return 1
    pid="$(cat "$PID_FILE" 2>/dev/null)" || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    cmd="$(ps -p "$pid" -o args= 2>/dev/null)" || return 1
    [[ -n "$cmd" ]] || return 1
    [[ "$cmd" == *"bridge.ingest"* ]]
}

# 若已在运行则不再重复启动；pid 文件过期（进程已死或 pid 被复用）则删文件后重新启动
if ingest_alive; then
    echo "ingest 已在运行 (pid=$(cat "$PID_FILE"))"
    exit 0
fi
rm -f "$PID_FILE"

nohup "$PYTHON_BIN" -m bridge.ingest >>"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "ingest 已启动 pid=$(cat "$PID_FILE")，日志: $LOG_FILE"
