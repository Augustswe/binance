"""策略基类与上下文"""
from __future__ import annotations

from typing import Any


class StrategyContext:
    """策略计算的输入上下文 (每个交易对每次K线周期构建一次)"""

    def __init__(self, symbol: str, closes, highs, lows, volumes, last_price: float,
                 atr: float, atr_pct: float, vol_ratio: float, regime: str):
        self.symbol = symbol
        self.closes = closes
        self.highs = highs
        self.lows = lows
        self.volumes = volumes
        self.last_price = last_price
        self.atr = atr
        self.atr_pct = atr_pct
        self.vol_ratio = vol_ratio
        self.regime = regime  # ranging / trend_up / trend_down


class Strategy:
    """策略基类: compute 返回 [-1, 1] 评分, >0 看多, <0 看空"""

    name = "base"

    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.enabled = bool(self.params.get("enabled", True))

    def compute(self, ctx: StrategyContext) -> float:
        return 0.0

    def __repr__(self):
        return f"<Strategy {self.name}>"
