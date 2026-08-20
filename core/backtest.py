"""回测引擎: 用历史K线逐根模拟策略执行, 输出绩效统计

与实盘交易逻辑保持一致:
- 每根K线收盘后计算策略信号 (复用 strategies/engine.py)
- 盘内检查 ATR 止盈止损 (用当根K线的最高/最低价模拟触发)
- 信号消失平仓, 手续费按 taker 双边收取
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .logger import get_logger
from strategies.engine import StrategyEngine

WARMUP_BARS = 60


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    bars: int = 0
    initial_balance: float = 10000.0
    final_equity: float = 0.0
    total_return: float = 0.0
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    params: dict = field(default_factory=dict)

    def stats(self) -> dict:
        n = len(self.trades)
        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        eq = np.array(self.equity_curve, dtype=float)
        peak = np.maximum.accumulate(eq)
        dd = (eq - peak) / peak
        max_dd = float(-dd.min()) if len(dd) else 0.0
        # 每根K线收益率 → Sharpe (年化, 按 5m=288根/日 计)
        rets = np.diff(eq) / np.maximum(eq[:-1], 1e-9)
        bars_per_year = 288 * 365 if self.timeframe == "5m" else 1440 * 365 // 300
        sharpe = 0.0
        if len(rets) > 2 and rets.std() > 0:
            sharpe = float(rets.mean() / rets.std() * np.sqrt(bars_per_year))
        total_pnl = sum(t["pnl"] for t in self.trades)
        fees = sum(t["fees"] for t in self.trades)
        return {
            "bars": self.bars,
            "trades": n,
            "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "total_return_pct": round(self.total_return * 100, 2),
            "final_equity": round(self.final_equity, 2),
            "total_pnl": round(total_pnl, 2),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "sharpe": round(sharpe, 2),
            "fees_total": round(fees, 2),
            "avg_trade_pnl": round(total_pnl / n, 2) if n else 0.0,
        }


class Backtester:
    def __init__(self, cfg: dict, symbol: str, timeframe: str, initial_balance: float = 10000.0,
                 min_qty: float = 0.0, step_size: float = 0.000001):
        self.cfg = cfg
        self.symbol = symbol
        self.timeframe = timeframe
        self.initial_balance = initial_balance
        self.min_qty = min_qty
        self.step_size = step_size
        self.fee_rate = float(cfg["fees"]["taker"])
        self.log = get_logger("backtest")

    @staticmethod
    def _round_down(qty: float, step: float) -> float:
        """按 step 向下取整"""
        if step <= 0:
            return qty
        s = f"{step:.10f}".rstrip("0")
        dec = len(s.split(".")[1]) if "." in s else 0
        return int(qty * (10 ** dec)) / (10 ** dec)

    def run(self, klines: list[dict]) -> BacktestResult:
        """逐根K线回测"""
        result = BacktestResult(
            symbol=self.symbol, timeframe=self.timeframe,
            bars=len(klines), initial_balance=self.initial_balance,
            params=self._params_snapshot(),
        )
        if len(klines) < WARMUP_BARS + 50:
            self.log.error("K线数量不足: %d", len(klines))
            return result

        engine = StrategyEngine(self.cfg)
        signal_cfg = self.cfg["signal"]
        pos_cfg = self.cfg["position"]
        risk_cfg = self.cfg["risk"]
        open_thr = float(signal_cfg["open_threshold"])
        close_thr = float(signal_cfg["close_threshold"])
        tp_atr = float(pos_cfg["tp_atr"])
        sl_atr = float(pos_cfg["sl_atr"])
        max_single = float(risk_cfg["max_single_order_notional"])
        max_total = float(risk_cfg["max_total_position_notional"])

        balance = self.initial_balance
        position = None  # dict: side/qty/entry/leverage/notional/tp/sl/atr/opened_at
        exposure = 0.0
        equity_curve: list[float] = []
        trades: list[dict] = []

        for i in range(WARMUP_BARS, len(klines)):
            bar = klines[i]
            close, high, low = bar["close"], bar["high"], bar["low"]

            # ---- 1) 盘中止盈止损 ----
            if position is not None:
                tp, sl = position["tp"], position["sl"]
                if position["side"] == "LONG":
                    if tp and high >= tp:
                        self._close(trades, position, tp, "止盈TP", balance)
                        balance = self._settle(position, tp, balance)
                        position, exposure = None, 0.0
                    elif sl and low <= sl:
                        self._close(trades, position, sl, "止损SL", balance)
                        balance = self._settle(position, sl, balance)
                        position, exposure = None, 0.0
                else:
                    if tp and low <= tp:
                        self._close(trades, position, tp, "止盈TP", balance)
                        balance = self._settle(position, tp, balance)
                        position, exposure = None, 0.0
                    elif sl and high >= sl:
                        self._close(trades, position, sl, "止损SL", balance)
                        balance = self._settle(position, sl, balance)
                        position, exposure = None, 0.0

            # ---- 2) 策略信号 (用截至当根K线的窗口) ----
            window = klines[max(0, i - self.cfg.get("kline_limit", 300) + 1): i + 1]
            analysis = engine.analyze(self.symbol, window)
            combined = analysis.combined if analysis else 0.0
            atr = analysis.atr if analysis else 0.0
            leverage = analysis.leverage if analysis else 1

            if position is None:
                # ---- 开仓 (锁定保证金) ----
                # trend_only: 只在趋势市顺势开仓 (trend_up只做多, trend_down只做空, 震荡市不开)
                trend_only = bool(signal_cfg.get("trend_only", False))
                regime = analysis.regime if analysis else "ranging"
                can_long = (not trend_only) or regime == "trend_up"
                can_short = (not trend_only) or regime == "trend_down"
                if combined >= open_thr and can_long:
                    position = self._open(close, "LONG", leverage, atr, tp_atr, sl_atr,
                                          balance, max_single, max_total, exposure)
                    if position:
                        balance -= position["notional"] / position["leverage"]
                        exposure = position["notional"]
                elif combined <= -open_thr and can_short:
                    position = self._open(close, "SHORT", leverage, atr, tp_atr, sl_atr,
                                          balance, max_single, max_total, exposure)
                    if position:
                        balance -= position["notional"] / position["leverage"]
                        exposure = position["notional"]
            else:
                # ---- 信号消失平仓 (close_threshold<=0 表示禁用, 只靠止盈止损离场) ----
                if close_thr > 0:
                    if position["side"] == "LONG" and combined < close_thr:
                        self._close(trades, position, close, "信号消失", balance)
                        balance = self._settle(position, close, balance)
                        position, exposure = None, 0.0
                    elif position["side"] == "SHORT" and combined > -close_thr:
                        self._close(trades, position, close, "信号消失", balance)
                        balance = self._settle(position, close, balance)
                        position, exposure = None, 0.0

            # ---- 3) 记录权益 ----
            equity = balance
            if position is not None:
                sign = 1.0 if position["side"] == "LONG" else -1.0
                upnl = (close - position["entry"]) * position["qty"] * sign
                equity += upnl
            equity_curve.append(round(equity, 2))

        # 回测结束: 按最后收盘价平掉剩余仓位
        if position is not None:
            last = klines[-1]["close"]
            self._close(trades, position, last, "回测结束", balance)
            balance = self._settle(position, last, balance)

        result.final_equity = round(balance, 2)
        result.total_return = (balance - self.initial_balance) / self.initial_balance
        result.trades = trades
        result.equity_curve = equity_curve
        return result

    # ---------------- 辅助 ----------------
    def _open(self, price, side, leverage, atr, tp_atr, sl_atr,
              balance, max_single, max_total, exposure) -> dict | None:
        budget = min(max_single, max_total - exposure)
        if budget <= 0:
            return None
        qty = self._round_down(budget / price, self.step_size)
        if qty <= 0 or (self.min_qty > 0 and qty < self.min_qty):
            return None
        notional = qty * price
        sign = 1.0 if side == "LONG" else -1.0
        # 止盈止损最小距离: 保证至少覆盖 2.5倍/1.5倍 双边手续费, 防止止盈被手续费吃掉
        pos_cfg = self.cfg.get("position", {})
        min_tp = price * float(pos_cfg.get("min_tp_pct", 0.0025))
        min_sl = price * float(pos_cfg.get("min_sl_pct", 0.0015))
        tp_dist = max(tp_atr * atr, min_tp)
        sl_dist = max(sl_atr * atr, min_sl)
        return {
            "side": side, "qty": qty, "entry": price, "leverage": leverage,
            "notional": notional, "tp": price + sign * tp_dist,
            "sl": price - sign * sl_dist, "atr": atr,
            "opened_at": time.time(),
        }

    def _settle(self, position, exit_price, balance) -> float:
        """平仓结算: 返回更新后的可用余额"""
        sign = 1.0 if position["side"] == "LONG" else -1.0
        gross = (exit_price - position["entry"]) * position["qty"] * sign
        fees = (position["entry"] * position["qty"] + exit_price * position["qty"]) * self.fee_rate
        pnl = gross - fees
        margin = position["notional"] / position["leverage"]
        return balance + margin + pnl

    def _close(self, trades, position, exit_price, reason, balance):
        sign = 1.0 if position["side"] == "LONG" else -1.0
        gross = (exit_price - position["entry"]) * position["qty"] * sign
        fees = (position["entry"] * position["qty"] + exit_price * position["qty"]) * self.fee_rate
        pnl = gross - fees
        pnl_pct = pnl / position["notional"] * 100 if position["notional"] else 0.0
        trades.append({
            "ts": time.time(), "symbol": self.symbol, "side": position["side"],
            "qty": position["qty"], "entry": position["entry"], "exit": exit_price,
            "leverage": position["leverage"], "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2), "fees": round(fees, 4), "reason": reason,
        })

    def _params_snapshot(self) -> dict:
        return {
            "signal": dict(self.cfg.get("signal", {})),
            "position": dict(self.cfg.get("position", {})),
            "strategies": {k: dict(v) for k, v in self.cfg.get("strategies", {}).items()},
        }
