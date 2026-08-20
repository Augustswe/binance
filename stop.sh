#!/usr/bin/env bash
# binance-quant 停止服务 (macOS / Linux)
# 用法: ./stop.sh
PORT=8090
PIDS=$(lsof -ti tcp:$PORT 2>/dev/null)
if [ -n "$PIDS" ]; then
  # shellcheck disable=SC2086
  kill -9 $PIDS 2>/dev/null && echo "✅ 已停止服务 (PID: $PIDS)" || echo "停止失败"
else
  if pkill -f "run.py" 2>/dev/null; then
    echo "✅ 已停止"
  else
    echo "服务未在运行 (端口 $PORT 无监听)"
  fi
fi
