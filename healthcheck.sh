#!/usr/bin/env bash
# 健康检查：进程存活 / 凭证存在 / 队列可读。进程不在且有凭证时自动重启。
# 判活必须校验进程命令行含 bridge.ingest：只看 pid 存活会被 pid 复用骗过
# （2026-10-23 / 2026-10-28 两次事故：ingest 死后 pid 被 Node/chrome-headless
# 进程回收复用，kill -0 误报存活，自动修复链被堵死）。
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${FEISHU_BRIDGE_DATA_DIR:-$HOME/feishu-bridge-data}"
PID_FILE="$DATA_DIR/ingest.pid"
CRED_FILE="$DATA_DIR/credentials.json"

cd "$PROJECT_DIR"  # 让 python3 -m bridge.* 能解析到包

STATUS="OK"
NEEDS_REPAIR=0

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

# 1) 凭证检查
if [[ ! -f "$CRED_FILE" ]]; then
    STATUS="DOWN-CREDENTIALS-MISSING"
    echo "[check] 凭证文件不存在: $CRED_FILE"
fi

# 2) 进程存活检查（命令行校验）
if ingest_alive; then
    PID="$(cat "$PID_FILE")"
    echo "[check] ingest 进程存活 pid=$PID（命令行已校验）"
else
    if [[ -f "$PID_FILE" ]]; then
        echo "[check] ingest 不在：pid 文件指向的进程不是 bridge.ingest（已死或 pid 被复用），只删 pid 文件、不 kill 该 pid"
        rm -f "$PID_FILE"
    else
        echo "[check] ingest 进程不在 (pid文件=${PID_FILE:-无})"
    fi
    if [[ -f "$CRED_FILE" ]]; then
        echo "[check] 凭证存在，尝试重启..."
        bash "$PROJECT_DIR/run_ingest.sh"
        # 重启后确认进程是否真的起来了（同样校验命令行）
        if ingest_alive; then
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
