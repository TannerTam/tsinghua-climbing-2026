#!/usr/bin/env bash
# Tsinghua Climbing 2026 - Linux / macOS 启动脚本
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================================"
echo "  Tsinghua Climbing 2026 - 启动脚本"
echo "============================================================"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 未检测到 python3, 请先安装 Python 3.9+。"
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "[1/3] 创建虚拟环境 .venv ..."
    python3 -m venv .venv
fi

echo "[2/3] 安装 / 更新依赖 ..."
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

echo "[3/3] 启动服务 ..."
echo "------------------------------------------------------------"
echo "  本机地址   : http://127.0.0.1:5000"
echo "  局域网地址可由下面其中一个地址访问:"
if command -v hostname >/dev/null 2>&1; then
    hostname -I 2>/dev/null | tr ' ' '\n' | sed '/^$/d' | awk '{ print "    http://" $1 ":5000" }' || true
fi
if command -v ifconfig >/dev/null 2>&1; then
    ifconfig | awk '/inet /{ if ($2 != "127.0.0.1") print "    http://" $2 ":5000" }' || true
fi
echo "------------------------------------------------------------"

export TSINGHUA_CLIMBING_HOST="${TSINGHUA_CLIMBING_HOST:-0.0.0.0}"
export TSINGHUA_CLIMBING_PORT="${TSINGHUA_CLIMBING_PORT:-5000}"
exec python server.py
