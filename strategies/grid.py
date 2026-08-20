"""网格策略: 以近期均价中枢为锚, 低于中枢买入(看多评分), 高于中枢卖出(看空评分)"""
from __future__ import annotations

import numpy as np

from .base import Strategy, StrategyContext


class GridStrategy(Strategy):
    name = "grid"

    def compute(self, ctx: StrategyContext) -> float:
        closes = np.asarray(ctx.closes, dtype=float)
        n = len(closes)
        if n < 20:
            return 0.0
        levels = max(1, int(self.params.get("levels", 10)))
        grid_pct = float(self.params.get("grid_pct", 0.02))

        # 中枢: 最近 min(n, 200) 根K线的均值 (EMA 权重更贴近当前)
        window = closes[-min(n, 200):]
        mid = float(np.mean(window))
        price = ctx.last_price
        if price <= 0 or mid <= 0:
            return 0.0

        # 偏离格数: 每偏离一格 grid_pct
        dist_grids = (mid - price) / price / grid_pct
        score = float(np.clip(dist_grids / levels, -1.0, 1.0))
        return score
