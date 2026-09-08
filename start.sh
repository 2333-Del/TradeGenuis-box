#!/usr/bin/env bash
# TradeGenuis · 箱体突破看板 一键启动
# 用法：bash start.sh          （依赖未装会自动装，服务起在 http://127.0.0.1:8808）
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ 未找到 python3，请先安装 Python 3.9+"; exit 1
fi

# 1) 依赖检查
if ! $PY -c "import requests, psycopg, psycopg_pool, exchange_calendars" >/dev/null 2>&1; then
  echo "⏳ 安装行情与历史复盘依赖 …"
  $PY -m pip install -q -r requirements.txt || { echo "❌ 安装失败，请手动执行: pip install -r requirements.txt"; exit 1; }
fi

PORT="${PORT:-8808}"
HOST="${HOST:-127.0.0.1}"

# 首次扫描由 server.py 统一触发，避免两个进程同时覆盖最新结果。

# 3) 启动看板服务
echo "🚀 TradeGenuis 箱体突破看板启动中…"
echo "   地址: http://${HOST}:${PORT}"
echo "   停止: Ctrl+C"
exec $PY server.py --host "$HOST" --port "$PORT"
