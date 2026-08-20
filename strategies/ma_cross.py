"""均线交叉策略: (EMA快 - EMA慢) 以 ATR 归一化, 快线斜率确认方向"""
from __future__ import annotations

import numpy as np

from core.indicators import ema
from .base import Strategy, StrategyContext


class MACrossStrategy(Strategy):
    name = "ma_cross"

    def compute(self, ctx: StrategyContext) -> float:
        closes = np.asarray(ctx.closes, dtype=float)
        n = len(closes)
        if n < 30:
            return 0.0
        fast_p = int(self.params.get("fast", 8))
        slow_p = int(self.params.get("slow", 21))
        if n < slow_p + 1:
            return 0.0

        ef = ema(closes, fast_p)
        es = ema(closes, slow_p)
        diff = float(ef[-1] - es[-1])
        denom = ctx.atr if ctx.atr and ctx.atr > 0 else max(ctx.last_price * 0.001, 1e-9)
        score = float(np.clip(diff / denom, -1.0, 1.0))

        # 快线斜率确认 (最近5根EMA的走势)
        if n >= fast_p + 5:
            slope = float(ef[-1] - ef[-6])
            if (diff > 0) != (slope > 0):
                score *= 0.4  # 方向未确认时大幅减弱
        return score
