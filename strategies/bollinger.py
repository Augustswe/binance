"""布林带策略: 震荡市做均值回归, 趋势市做突破顺势"""
from __future__ import annotations

import numpy as np

from core.indicators import bollinger
from .base import Strategy, StrategyContext


class BollingerStrategy(Strategy):
    name = "bollinger"

    def compute(self, ctx: StrategyContext) -> float:
        closes = np.asarray(ctx.closes, dtype=float)
        n = len(closes)
        if n < 30:
            return 0.0
        period = int(self.params.get("period", 20))
        num_std = float(self.params.get("std", 2.0))
        mid, upper, lower = bollinger(closes, period, num_std)
        price = ctx.last_price
        if np.isnan(mid) or np.isnan(upper) or np.isnan(lower):
            return 0.0
        band = (upper - lower) / 2.0
        if band <= 0:
            return 0.0

        if ctx.regime == "ranging":
            # 均值回归: 触下轨买、触上轨卖
            return float(np.clip((mid - price) / band, -1.0, 1.0))
        # 趋势市: 顺势突破才给强信号
        direction = 1 if ctx.regime == "trend_up" else -1
        if direction == 1 and price > upper:
            return 1.0
        if direction == -1 and price < lower:
            return -1.0
        # 趋势中的回调: 给轻微逆势分 (与均线策略叠加时被权重稀释)
        return float(np.clip((mid - price) / band * 0.3, -0.3, 0.3)) * -direction
