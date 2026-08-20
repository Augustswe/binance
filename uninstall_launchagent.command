#!/bin/bash
# 双击本文件: 取消 binance-quant 的 launchd 自启注册 (停止由 launchd 托管的服务)
set -e
LABEL="com.binance-quant.trading"
UIDN=$(id -u)

echo "== 停止 launchd 托管的 binance-quant =="
launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true
echo "已卸载。若想重新启用, 双击 install_launchagent.command。"
