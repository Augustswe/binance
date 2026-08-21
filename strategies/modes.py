"""多模式管理器: 统一 6 种策略模式的接口, 支持并行开仓与按模式统计

模式:
- donchian   通道突破趋势跟踪 (长周期, 移动止损锁利)
- multi      多策略自适应合成 (网格/均线/RSI/布林带 按市场状态加权)
- grid       网格 (震荡市专用)
- ma_cross   均线交叉
- rsi        RSI 超买超卖
- bollinger  布林带

同币种竞争制: 同一币种同一时间只允许一个模式开仓 (交易所仓位唯一),
但不同币种可被不同模式瓜分, 每个模式独立统计盈亏 → 系统学习分配资金权重。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from core.indicators import atr_series, trend_strength, vol_ratio
from core.logger import get_logger
from strategies.base import StrategyContext
from strategies.leverage import compute_leverage
from strategies.bollinger import BollingerStrategy
from strategies.donchian import DonchianEngine
from strategies.grid import GridStrategy
from strategies.ma_cross import MACrossStrategy
from strategies.rsi import RSIStrategy
from strategies.engine import StrategyEngine

ALL_MODES = ["donchian", "multi", "grid", "ma_cross", "rsi", "bollinger"]
# 单策略模式 (需要自己构建 ctx 算评分)
SINGLE_STRATEGIES = {"grid", "ma_cross", "rsi", "bollinger"}

OPEN_TH_DEFAULT = 0.15  # |评分| 超过此值开仓 (multi 和单策略模式共用)


@dataclass
class ModeSignal:
    """统一模式信号"""
    mode: str
    action: str | None        # LONG / SHORT / None
    score: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0
    leverage: int = 1
    strength: float = 0.0     # 信号强度 0~1
    tp_atr: float = 0.0       # 固定止盈倍数 (donchian=0 表示不用固定TP)
    sl_atr: float = 0.0       # 止损倍数
    sl: float = 0.0           # 计算好的止损价 (donchian 用)
    regime: str = "ranging"
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "action": self.action or "等待",
            "regime": self.regime,
            "score": round(self.score, 3),
            "combined": round(self.score, 3),
            "atr": round(self.atr, 6),
            "atr_pct": round(self.atr_pct * 100, 3),
            "strength": round(self.strength, 2),
            "leverage": self.leverage,
            "sl": round(self.sl, 6),
            "ts": self.ts,
        }


class ModeManager:
    """多模式引擎: 每个模式独立分析, 返回带模式标签的信号"""

    def __init__(self, cfg: dict, enabled_modes: list[str] | None = None):
        self.cfg = cfg
        self.log = get_logger("modes")
        # 启用模式: config.modes.enabled 优先, 否则回退 strategy_mode
        mcfg = cfg.get("modes", {})
        if enabled_modes is not None:
            self.enabled = [m for m in enabled_modes if m in ALL_MODES]
        elif mcfg.get("enabled"):
            self.enabled = [m for m in mcfg["enabled"] if m in ALL_MODES]
        else:
            self.enabled = [cfg.get("strategy_mode", "donchian")] if cfg.get("strategy_mode") in ALL_MODES else ["donchian"]
        if not self.enabled:
            self.enabled = ["donchian"]
        self.log.info("启用策略模式: %s", self.enabled)

        self.donchian = DonchianEngine(cfg) if "donchian" in self.enabled else None
        self.multi = StrategyEngine(cfg) if "multi" in self.enabled else None
        self.singles: dict[str, object] = {}
        if "grid" in self.enabled:
            self.singles["grid"] = GridStrategy(cfg["strategies"].get("grid", {}))
        if "ma_cross" in self.enabled:
            self.singles["ma_cross"] = MACrossStrategy(cfg["strategies"].get("ma_cross", {}))
        if "rsi" in self.enabled:
            self.singles["rsi"] = RSIStrategy(cfg["strategies"].get("rsi", {}))
        if "bollinger" in self.enabled:
            self.singles["bollinger"] = BollingerStrategy(cfg["strategies"].get("bollinger", {}))
        # 资金权重 (模式学习器更新): mode -> weight, 权重高的多给资金
        self.weights: dict[str, float] = dict(mcfg.get("weights", {}))

    # ---------------- 资金权重 ----------------
    def weight_of(self, mode: str) -> float:
        """该模式当前资金权重 (默认1.0, 学习器按实盘表现调整)"""
        w = self.weights.get(mode)
        return float(w) if w and w > 0.1 else 1.0

    def update_weights(self, weights: dict[str, float]) -> None:
        self.weights = {k: float(v) for k, v in weights.items() if k in self.enabled}
        self.cfg.setdefault("modes", {})["weights"] = dict(self.weights)

    # ---------------- 分析 ----------------
    def analyze(self, mode: str, symbol: str, klines: list[dict],
                price: float | None = None) -> ModeSignal | None:
        """对单个模式执行一次分析, 返回统一信号"""
        if mode not in self.enabled:
            return None
        ts = klines[-1]["open_time"] / 1000.0 if klines else 0.0
        if mode == "donchian" and self.donchian:
            sig = self.donchian.analyze(symbol, klines)
            if sig is None:
                return None
            return ModeSignal(
                mode="donchian", action=sig.action, score=(1.0 if sig.action == "LONG" else -1.0 if sig.action == "SHORT" else 0.0),
                atr=sig.atr, atr_pct=sig.atr / sig.entry if sig.entry else 0.0,
                leverage=sig.leverage, strength=sig.strength,
                sl_atr=self.donchian.sl_atr, sl=sig.sl, regime=sig.regime, ts=sig.ts,
            )
        if mode == "multi" and self.multi:
            res = self.multi.analyze(symbol, klines)
            if res is None:
                return None
            return ModeSignal(
                mode="multi", action=("LONG" if res.combined >= OPEN_TH_DEFAULT else "SHORT" if res.combined <= -OPEN_TH_DEFAULT else None),
                score=res.combined, atr=res.atr, atr_pct=res.atr_pct,
                leverage=res.leverage, strength=abs(res.combined),
                tp_atr=float(self.cfg["position"]["tp_atr"]),
                sl_atr=float(self.cfg["position"]["sl_atr"]),
                regime=res.regime, ts=res.ts,
            )
        if mode in SINGLE_STRATEGIES and mode in self.singles:
            ctx = self._build_ctx(mode, symbol, klines)
            if ctx is None:
                return None
            strat = self.singles[mode]
            score = float(strat.compute(ctx))
            action = "LONG" if score >= OPEN_TH_DEFAULT else ("SHORT" if score <= -OPEN_TH_DEFAULT else None)
            lev_cfg = self.cfg.get("leverage", {})
            lev_min = int(lev_cfg.get("min", 1))
            lev_max = int(lev_cfg.get("max", 5))
            pos_sl_atr = float(self.cfg.get("position", {}).get("sl_atr", 2.7))
            sl_pct = pos_sl_atr * ctx.atr / ctx.last_price if ctx.last_price > 0 else 0.0
            lev = compute_leverage(abs(score), sl_pct, lev_min, lev_max)
            return ModeSignal(
                mode=mode, action=action, score=score, atr=ctx.atr, atr_pct=ctx.atr_pct,
                leverage=lev, strength=abs(score),
                tp_atr=float(self.cfg["position"]["tp_atr"]),
                sl_atr=float(self.cfg["position"]["sl_atr"]),
                regime=ctx.regime, ts=ts,
            )
        return None

    def _build_ctx(self, mode: str, symbol: str, klines: list[dict]) -> StrategyContext | None:
        """为单策略模式构建计算上下文 (复用 multi 引擎的指标逻辑)"""
        if len(klines) < 60:
            return None
        closes = np.array([k["close"] for k in klines], dtype=float)
        highs = np.array([k["high"] for k in klines], dtype=float)
        lows = np.array([k["low"] for k in klines], dtype=float)
        volumes = np.array([k["volume"] for k in klines], dtype=float)
        last_price = float(closes[-1])
        if last_price <= 0:
            return None
        atr_arr = atr_series(highs, lows, closes, period=14)
        atr_now = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else last_price * 0.001
        atr_pct = atr_now / last_price
        vr = vol_ratio(atr_arr, lookback=100)
        ts = trend_strength(closes, fast=8, slow=21)
        ratio = abs(ts) / atr_pct if atr_pct > 0 else 0.0
        regime = "trend_up" if ts > 0 and ratio >= 0.30 else ("trend_down" if ts < 0 and ratio >= 0.30 else "ranging")
        return StrategyContext(
            symbol=symbol, closes=closes, highs=highs, lows=lows, volumes=volumes,
            last_price=last_price, atr=atr_now, atr_pct=atr_pct,
            vol_ratio=vr, regime=regime,
        )
