"""策略引擎: 识别市场状态(震荡/上涨/下跌), 自动分配策略权重, 计算综合信号与动态杠杆"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from core.indicators import atr_series, trend_strength, vol_ratio
from core.logger import get_logger
from .base import StrategyContext
from .bollinger import BollingerStrategy
from .grid import GridStrategy
from .ma_cross import MACrossStrategy
from .rsi import RSIStrategy

# 不同市场状态下各策略的权重
REGIME_WEIGHTS = {
    "ranging": {"grid": 0.50, "ma_cross": 0.10, "rsi": 0.25, "bollinger": 0.15},
    "trend_up": {"grid": 0.05, "ma_cross": 0.50, "rsi": 0.10, "bollinger": 0.35},
    "trend_down": {"grid": 0.05, "ma_cross": 0.50, "rsi": 0.10, "bollinger": 0.35},
}

TREND_RATIO_THRESHOLD = 0.30  # |趋势强度|/ATR% 超过此值视为趋势市


@dataclass
class AnalysisResult:
    symbol: str
    regime: str
    scores: dict = field(default_factory=dict)
    weights: dict = field(default_factory=dict)
    combined: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0
    vol_ratio: float = 1.0
    leverage: int = 1
    dominant: str = "none"
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "regime": self.regime,
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "weights": {k: round(v, 2) for k, v in self.weights.items()},
            "combined": round(self.combined, 3),
            "atr": round(self.atr, 6),
            "atr_pct": round(self.atr_pct * 100, 3),   # 百分比
            "vol_ratio": round(self.vol_ratio, 2),
            "leverage": self.leverage,
            "dominant": self.dominant,
            "ts": self.ts,
        }


class StrategyEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.log = get_logger("engine")
        scfg = cfg["strategies"]
        self.strategies: dict[str, object] = {
            "grid": GridStrategy(scfg.get("grid", {})),
            "ma_cross": MACrossStrategy(scfg.get("ma_cross", {})),
            "rsi": RSIStrategy(scfg.get("rsi", {})),
            "bollinger": BollingerStrategy(scfg.get("bollinger", {})),
        }

    def analyze(self, symbol: str, klines: list[dict]) -> AnalysisResult | None:
        """对单个交易对做完整分析"""
        if len(klines) < 60:
            return None
        closes = np.array([k["close"] for k in klines], dtype=float)
        highs = np.array([k["high"] for k in klines], dtype=float)
        lows = np.array([k["low"] for k in klines], dtype=float)
        volumes = np.array([k["volume"] for k in klines], dtype=float)
        last_price = float(closes[-1])
        if last_price <= 0:
            return None

        # ---- 指标 ----
        atr_arr = atr_series(highs, lows, closes, period=14)
        atr_now = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else last_price * 0.001
        atr_pct = atr_now / last_price
        vr = vol_ratio(atr_arr, lookback=100)
        ts = trend_strength(closes, fast=8, slow=21)

        # ---- 市场状态 ----
        ratio = abs(ts) / atr_pct if atr_pct > 0 else 0.0
        if ratio >= TREND_RATIO_THRESHOLD:
            regime = "trend_up" if ts > 0 else "trend_down"
        else:
            regime = "ranging"

        # ---- 策略评分 ----
        ctx = StrategyContext(
            symbol=symbol, closes=closes, highs=highs, lows=lows, volumes=volumes,
            last_price=last_price, atr=atr_now, atr_pct=atr_pct,
            vol_ratio=vr, regime=regime,
        )
        scores: dict[str, float] = {}
        for name, strat in self.strategies.items():
            if not strat.enabled:
                continue
            try:
                scores[name] = float(strat.compute(ctx))
            except Exception as e:
                self.log.error("[%s] 策略 %s 计算异常: %s", symbol, name, e)
                scores[name] = 0.0

        # ---- 综合评分 (加权) ----
        weights = REGIME_WEIGHTS[regime]
        total_w = sum(w for n, w in weights.items() if n in scores)
        combined = 0.0
        if total_w > 0:
            combined = sum(scores.get(n, 0.0) * w for n, w in weights.items() if n in scores) / total_w

        # 主导策略: |评分| 最大的策略
        dominant = "none"
        if scores:
            dominant = max(scores, key=lambda n: abs(scores[n]))

        # ---- 动态杠杆 (波动率越高杠杆越低) ----
        lev_cfg = self.cfg.get("leverage", {})
        lev_min = int(lev_cfg.get("min", 1))
        lev_max = int(lev_cfg.get("max", 5))
        if lev_cfg.get("mode", "auto") == "fixed":
            leverage = max(lev_min, min(lev_max, int(lev_cfg.get("fixed", lev_max))))
        else:
            # 基准3倍, 波动放大则降杠杆, 波动收窄可升杠杆
            lev = round(3.0 / max(vr, 0.4))
            leverage = max(lev_min, min(lev_max, lev))

        return AnalysisResult(
            symbol=symbol, regime=regime, scores=scores, weights=weights,
            combined=float(combined), atr=atr_now, atr_pct=atr_pct,
            vol_ratio=vr, leverage=leverage, dominant=dominant,
            ts=klines[-1]["close_time"] / 1000.0,
        )
