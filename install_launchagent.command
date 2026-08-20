#!/bin/bash
# 双击本文件: 把 binance-quant 注册为 launchd 用户代理 (登录自启 + 掉线自动重拉)
# 必须在你本机 GUI 会话下运行 (双击 / 本机 Terminal), 不能在 agent 沙箱里跑 (会报 EIO)。
set -e
PLIST="$HOME/Library/LaunchAgents/com.binance-quant.trading.plist"
LABEL="com.binance-quant.trading"
UIDN=$(id -u)

echo "== 1. 停掉手动占用 8090 的旧进程 =="
PID=$(lsof -tiTCP:8090 -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PID" ]; then
  kill "$PID" 2>/dev/null || true
  sleep 1
  echo "已结束旧进程 PID=$PID"
else
  echo "无占用, 跳过"
fi

echo "== 2. 卸载旧注册 (若已存在) =="
launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true

echo "== 3. 注册并启动 (bootstrap) =="
launchctl bootstrap "gui/$UIDN" "$PLIST"
launchctl kickstart "gui/$UIDN/$LABEL"

echo "== 完成 =="
echo "服务已由 launchd 托管: 登录自启 + 掉线自动重拉 (KeepAlive=true)。"
echo "查看状态: launchctl list | grep binance"
echo "停止服务: launchctl bootout gui/$UIDN/$LABEL"
