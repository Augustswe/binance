"""Donchian 通道突破趋势跟踪策略 (海龟/CTA 风格, 长周期专用)

- 入场: 收盘价突破前 entry_n 根K线最高价 → 做多; 跌破前 entry_n 根最低价 → 做空
- 出场: 价格反向突破前 exit_n 根通道 → 平仓 (让利润奔跑, 无固定止盈)
- 止损: 入场价 ∓ sl_atr × ATR (动态波动止损)
"""
from __future__ import annotations

import time

import numpy as np

from core.indicators import atr
from core.logger import get_logger


class DonchianSignal:
    """一次分析结果: action 为 LONG/SHORT/None"""

    def __init__(self, action: str | None, entry: float, sl: float, atr: float,
                 regime: str, ts: float, up_level: float, dn_level: float,
                 strength: float = 0.0, leverage: int = 1):
        self.action = action
        self.entry = entry
        self.sl = sl
        self.atr = atr
        self.regime = regime
        self.ts = ts
        self.up_level = up_level
        self.dn_level = dn_level
        self.strength = strength    # 信号强度 0~1 (突破深度相对 ATR)
        self.leverage = leverage    # 动态杠杆 (强信号高倍, 弱信号低倍试错)

    def to_dict(self) -> dict:
        return {
            "mode": "donchian",
            "action": self.action or "等待",
            "regime": self.regime,
            "scores": {"donchian": 1.0 if self.action else 0.0},
            "combined": 1.0 if self.action == "LONG" else (-1.0 if self.action == "SHORT" else 0.0),
            "atr": round(self.atr, 6),
            "atr_pct": round(self.atr / self.entry * 100, 3) if self.entry else 0.0,
            "entry": round(self.entry, 6),
            "sl": round(self.sl, 6),
            "up_level": round(self.up_level, 6),
            "dn_level": round(self.dn_level, 6),
            "strength": round(self.strength, 2),
            "leverage": self.leverage,
            "ts": self.ts,
        }


class DonchianEngine:
    def __init__(self, cfg: dict):
        d = cfg.get("donchian", {})
        self.entry_n = int(d.get("entry_n", 55))
        self.exit_n = int(d.get("exit_n", 20))
        self.sl_atr = float(d.get("sl_atr", 2.5))
        # 动态杠杆配置: 强信号高倍开单, 弱信号低倍试错
        lev_cfg = d.get("leverage", {}) or {}
        self.lev_min = int(lev_cfg.get("min", 1))
        self.lev_max = int(lev_cfg.get("max", 5))
        self.log = get_logger("donchian")

    def set_params(self, combo: dict) -> None:
        """自动学习器切换组合时更新参数"""
        self.entry_n = int(combo.get("entry_n", self.entry_n))
        self.exit_n = int(combo.get("exit_n", self.exit_n))
        self.sl_atr = float(combo.get("sl_atr", self.sl_atr))
        self.log.info("Donchian 参数更新: entry=%d exit=%d sl=%.1f ATR",
                      self.entry_n, self.exit_n, self.sl_atr)

    def analyze(self, symbol: str, klines: list[dict]) -> DonchianSignal | None:
        n = len(klines)
        if n < self.entry_n + 5:
            return None
        closes = np.array([k["close"] for k in klines], dtype=float)
        highs = np.array([k["high"] for k in klines], dtype=float)
        lows = np.array([k["low"] for k in klines], dtype=float)
        c = float(closes[-1])
        a = atr(highs[-40:], lows[-40:], closes[-40:], 14)
        if not a or a <= 0:
            a = c * 0.01

        up_level = float(highs[-self.entry_n - 1:-1].max())
        dn_level = float(lows[-self.entry_n - 1:-1].min())
        strength = 0.0
        leverage = self.lev_min
        if c > up_level:
            action, sl = "LONG", c - self.sl_atr * a
            # 信号强度: 突破深度相对 ATR (0~1, 突破越深信号越强)
            strength = max(0.0, min(1.0, (c - up_level) / (a * 2.0)))
        elif c < dn_level:
            action, sl = "SHORT", c + self.sl_atr * a
            strength = max(0.0, min(1.0, (dn_level - c) / (a * 2.0)))
        else:
            action, sl = None, 0.0

        # 动态杠杆: 强信号高倍开单, 弱信号低倍试错
        # strength 0.15 以下 = 试探仓(最低杠杆), 0.6 以上 = 重仓(最高杠杆), 中间线性
        if action:
            t = max(0.0, min(1.0, (strength - 0.15) / 0.45))
            leverage = max(self.lev_min, min(
                self.lev_max, int(round(self.lev_min + t * (self.lev_max - self.lev_min)))
            ))
            # 风控: 止损距离不能太接近强平线 (杠杆过高会被先强平)
            # 强平距离 ≈ 100%/杠杆, 要求 止损距离 >= 50% 强平距离
            sl_pct = self.sl_atr * a / c if c > 0 else 0.0
            if sl_pct > 0:
                max_safe = max(1, int(0.5 / sl_pct))
                leverage = max(self.lev_min, min(leverage, max_safe))

        regime = "trend_up" if action == "LONG" else ("trend_down" if action == "SHORT" else "ranging")
        return DonchianSignal(
            action=action, entry=c, sl=sl, atr=a, regime=regime,
            ts=time.time(),   # 实际分析时间 (每5分钟刷新), 而非K线开盘时间
            up_level=up_level, dn_level=dn_level,
            strength=strength, leverage=leverage,
        )

    def check_exit(self, klines: list[dict], pos: dict) -> bool:
        """通道反向突破 → 平仓 (long 跌破 exit_n 最低, short 升破 exit_n 最高)"""
        n = len(klines)
        if n < self.exit_n + 2:
            return False
        lows = np.array([k["low"] for k in klines], dtype=float)
        highs = np.array([k["high"] for k in klines], dtype=float)
        c = float(klines[-1]["close"])
        if pos["side"] == "LONG":
            return c < float(lows[-self.exit_n - 1:-1].min())
        else:
            return c > float(highs[-self.exit_n - 1:-1].max())
