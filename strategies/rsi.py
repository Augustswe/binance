"""RSI 超买超卖策略: 超卖买、超买卖, 中性区弱回归"""
from __future__ import annotations

import numpy as np

from core.indicators import rsi
from .base import Strategy, StrategyContext


class RSIStrategy(Strategy):
    name = "rsi"

    def compute(self, ctx: StrategyContext) -> float:
        period = int(self.params.get("period", 14))
        r = rsi(ctx.closes, period)
        if np.isnan(r):
            return 0.0
        ob = float(self.params.get("overbought", 70))
        os_ = float(self.params.get("oversold", 30))

        if r <= os_:
            # 超卖 → 买入信号, 越超卖越强
            return float(np.clip((os_ - r) / os_ * 1.5, 0.0, 1.0))
        if r >= ob:
            # 超买 → 卖出信号
            return float(np.clip(-(r - ob) / (100.0 - ob) * 1.5, -1.0, 0.0))
        # 中性区: 轻微均值回归
        return float((50.0 - r) / 50.0 * 0.3)
