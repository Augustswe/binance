#!/usr/bin/env bash
# binance-quant 一键启动 (macOS 双击运行)
# 双击本文件即可: 启动服务并自动打开浏览器; 服务已在运行时只开浏览器
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
PORT=8090

# --- 解析 Python 解释器 ---
PY=""
if [ -x "$DIR/.venv/bin/python" ]; then
  PY="$DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "❌ 未找到 Python, 请先安装 Python 3.10+ 并加入 PATH"
  exit 1
fi

# --- 确保依赖已安装 (首次运行自动建虚拟环境并安装) ---
if ! "$PY" -c "import fastapi" >/dev/null 2>&1; then
  if [ ! -x "$DIR/.venv/bin/python" ]; then
    echo "📦 未检测到依赖, 正在创建虚拟环境并安装 (首次较慢)…"
    "$PY" -m venv "$DIR/.venv" || { echo "❌ 创建虚拟环境失败"; exit 1; }
  fi
  "$DIR/.venv/bin/python" -m pip install -U pip >/dev/null 2>&1
  "$DIR/.venv/bin/python" -m pip install -r "$DIR/requirements.txt" \
    || { echo "❌ 依赖安装失败, 请手动执行: pip install -r requirements.txt"; exit 1; }
  PY="$DIR/.venv/bin/python"
fi

# --- 已在运行? ---
if curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/api/state" 2>/dev/null; then
  echo "✅ 服务已在运行"
else
  mkdir -p "$DIR/logs"
  echo "⏳ 正在启动服务…"
  nohup "$PY" "$DIR/run.py" >> "$DIR/logs/console.log" 2>&1 &
  for i in $(seq 1 40); do
    if curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/api/state" 2>/dev/null; then break; fi
    sleep 1
  done
fi

echo "🌐 打开仪表盘: http://127.0.0.1:$PORT"
if command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:$PORT" 2>/dev/null &
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://127.0.0.1:$PORT" >/dev/null 2>&1 &
else
  echo "请手动打开 http://127.0.0.1:$PORT"
fi
