#!/usr/bin/env bash
# 启动飞书长连接接入进程（后台运行，日志与 pid 写入数据目录）。
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

# 若已在运行则不再重复启动
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "ingest 已在运行 (pid=$(cat "$PID_FILE"))"
    exit 0
fi

nohup "$PYTHON_BIN" -m bridge.ingest >>"$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "ingest 已启动 pid=$(cat "$PID_FILE")，日志: $LOG_FILE"
