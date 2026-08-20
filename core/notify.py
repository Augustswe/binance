"""Telegram 通知: 开平仓/熔断/调参等事件推送到手机

配置: .env 中 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
获取方式:
  - Bot Token: 在 Telegram 里找 @BotFather, 发 /newbot 创建机器人, 得到 token
  - Chat ID: 把机器人拉进群或私聊发消息后, 找 @userinfobot 查询你的 chat_id
"""
from __future__ import annotations

import requests

from .logger import get_logger

API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    def __init__(self, token: str = "", chat_id: str = "", enabled: bool = True):
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.enabled = enabled and bool(self.token and self.chat_id)
        self.log = get_logger("notify")
        if token and chat_id and not self.enabled:
            self.enabled = True  # 显式给了配置就启用

    def send(self, text: str) -> bool:
        """发送消息, 失败只记日志不抛异常"""
        if not self.enabled:
            self.log.debug("Telegram 未配置, 跳过通知: %s", text[:60])
            return False
        try:
            resp = requests.post(
                API.format(token=self.token),
                data={"chat_id": self.chat_id, "text": text},
                timeout=12,
            )
            data = resp.json()
            if data.get("ok"):
                self.log.debug("Telegram 已发送: %s", text[:60])
                return True
            self.log.warning("Telegram 发送失败: %s", data.get("description"))
            return False
        except Exception as e:
            self.log.warning("Telegram 连接失败: %s", e)
            return False

    def send_markdown(self, text: str) -> bool:
        """发送 Markdown 格式消息"""
        if not self.enabled:
            return self.send(text)
        try:
            resp = requests.post(
                API.format(token=self.token),
                data={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=12,
            )
            return bool(resp.json().get("ok"))
        except Exception:
            return self.send(text)


def fmt_pnl(pnl: float) -> str:
    return f"{pnl:+.2f} U"
