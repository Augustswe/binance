#!/bin/bash
# 手动启动/停止脚本 (不依赖 launchd, 任何环境可用)
# 用法: ./manual.sh start | stop | status | logs
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$DIR/logs/app.pid"
LOGFILE="$DIR/logs/console.log"

start() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "系统已在运行 (PID $(cat "$PIDFILE"))"
    return 0
  fi
  cd "$DIR"
  nohup .venv/bin/python run.py >> "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  sleep 8
  if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "✅ 系统已启动 (PID $(cat "$PIDFILE"))"
    echo "   仪表盘: http://127.0.0.1:8090"
  else
    echo "❌ 启动失败, 查看日志: tail -50 $LOGFILE"
  fi
}

stop() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
    echo "系统已停止"
  else
    pkill -f "binance-quant/run.py" 2>/dev/null && echo "系统已停止" || echo "系统未在运行"
  fi
}

status() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "✅ 运行中 (PID $(cat "$PIDFILE"))"
  elif pgrep -f "run.py" >/dev/null; then
    echo "✅ 运行中 (PID $(pgrep -f run.py | head -1), 但无PID文件)"
  else
    echo "❌ 未运行"
  fi
  curl -sS -m 3 -o /dev/null -w "仪表盘: HTTP %{http_code}\n" http://127.0.0.1:8090/api/state 2>/dev/null || echo "仪表盘: 未响应"
}

logs() {
  tail -40 "$LOGFILE" 2>/dev/null || echo "暂无日志"
}

case "${1:-status}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  logs) logs ;;
  *) echo "用法: $0 {start|stop|status|logs}" ;;
esac
