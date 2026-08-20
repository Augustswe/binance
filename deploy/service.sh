#!/bin/bash
# Binance 量化交易系统 24小时守护服务管理脚本 (macOS launchd)
# 用法: ./service.sh start | stop | status | logs | install | uninstall
set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.binance-quant.trading.plist"
LABEL="com.binance-quant.trading"

install() {
  cp "$DIR/deploy/com.binance-quant.trading.plist" "$PLIST"
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "✅ 已安装并启动 24h 守护服务"
}

uninstall() {
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已卸载守护服务"
}

start() {
  [ -f "$PLIST" ] || install
  launchctl kickstart "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl load "$PLIST"
  echo "✅ 服务已启动 (PID: $(pgrep -f 'binance-quant/run.py' | head -1))"
}

stop() {
  launchctl unload "$PLIST" 2>/dev/null || true
  pkill -f "binance-quant/run.py" 2>/dev/null || true
  echo "服务已停止"
}

status() {
  echo "--- launchd 服务 ---"
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state|program =" | head -3
  else
    echo "(未注册)"
  fi
  echo "--- 进程 ---"
  pgrep -fl "binance-quant/run.py" || echo "(未运行)"
  echo "--- Web 仪表盘 ---"
  curl -sS -m 3 -o /dev/null -w "http://127.0.0.1:8090 -> HTTP %{http_code}\n" http://127.0.0.1:8090/api/state || echo "未响应"
}

logs() {
  tail -40 "$DIR/logs/quant.log"
}

case "${1:-status}" in
  install) install ;;
  uninstall) uninstall ;;
  start) start ;;
  stop) stop ;;
  status) status ;;
  logs) logs ;;
  *) echo "用法: $0 {start|stop|status|logs|install|uninstall}" ;;
esac
