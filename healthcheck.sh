#!/usr/bin/env bash
# 健康检查：进程存活 / 凭证存在 / 队列可读。进程不在且有凭证时自动重启。
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${FEISHU_BRIDGE_DATA_DIR:-$HOME/feishu-bridge-data}"
PID_FILE="$DATA_DIR/ingest.pid"
CRED_FILE="$DATA_DIR/credentials.json"

cd "$PROJECT_DIR"  # 让 python3 -m bridge.* 能解析到包

STATUS="OK"
NEEDS_REPAIR=0

# 1) 凭证检查
if [[ ! -f "$CRED_FILE" ]]; then
    STATUS="DOWN-CREDENTIALS-MISSING"
    echo "[check] 凭证文件不存在: $CRED_FILE"
fi

# 2) 进程存活检查
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    PID="$(cat "$PID_FILE")"
    echo "[check] ingest 进程存活 pid=$PID"
else
    echo "[check] ingest 进程不在 (pid文件=${PID_FILE:-无})"
    if [[ -f "$CRED_FILE" ]]; then
        echo "[check] 凭证存在，尝试重启..."
        bash "$PROJECT_DIR/run_ingest.sh"
        # 重启后确认进程是否真的起来了
        if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            STATUS="REPAIRED"
        else
            STATUS="DOWN"
        fi
    else
        echo "[check] 无凭证，不重启（请先按 SETUP.md 配置）"
    fi
fi

# 3) 队列可读性检查
if ! python3 -m bridge.queue_store stats >/dev/null 2>&1; then
    echo "[check] 队列不可读"
    if [[ "$STATUS" == "OK" || "$STATUS" == "REPAIRED" ]]; then
        STATUS="DOWN"
    fi
else
    echo "[check] 队列正常，当前状态: $(python3 -m bridge.queue_store stats)"
fi

echo "STATUS: $STATUS"
