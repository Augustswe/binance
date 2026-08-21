"""交易状态: 资金、持仓、成交记录、权益曲线, 持久化到 data/state.json"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .logger import get_logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = DATA_DIR / "state.json"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_today_str() -> str:
    """用本地时区日期 (用户可见口径: 今日盈亏按本地0点算, 而非UTC)"""
    return datetime.now().strftime("%Y-%m-%d")


class TradingState:
    def __init__(self, cfg: dict, state_file: "Path | None" = None):
        self.cfg = cfg
        self.log = get_logger("state")
        self.lock = threading.RLock()
        self.state_file = Path(state_file) if state_file else STATE_FILE
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        initial = float(cfg.get("paper_initial_balance", 10000))
        today = utc_today_str()
        self.data = {
            "mode": cfg["mode"],
            "running": True,
            "paused": False,
            "halted": False,
            "halt_reason": None,
            "balance_cash": initial,          # paper: 可用现金; live: 钱包余额
            "day_start_equity": initial,
            "day_start_initialized": False,   # 首次读到真实权益后置 True
            "day_date": today,
            "positions": {},                  # symbol -> position dict
            "trades": [],                     # 已平仓记录
            "orders": [],                     # 成交流水: 下单/卖出 (交易所逐笔成交)
            "equity_history": [],             # [ts, equity]
            "prices": {},                     # symbol -> 最新价
            "mark_prices": {},                # symbol -> 标记价
            "change_24h": {},                 # symbol -> 24h涨跌幅
            "signals": {},                    # symbol -> 最近分析结果
            "last_close_time": {},            # symbol -> 上次平仓时间戳 (冷静期)
            "events": [],                     # 操作日志: [{ts, type, msg}]
            "last_tick_ts": 0.0,              # 主循环最近一次数据更新时间 (心跳)
            "strategy_stats": {
                "total_trades": 0,
                "wins": 0,
                "realized_pnl": 0.0,
                "fees_paid": 0.0,
            },
            "mode_stats": {},             # 按策略模式统计: mode -> {trades/wins/pnl/fees}
            "initial_capital": 0.0,       # 起始资金 (用户在设置中填, 用于盈亏模块; 0 表示未设置)
        }
        self._load()

    # ---------------- 持久化 ----------------
    def _load(self) -> None:
        if self.state_file.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for k in ("positions", "trades", "orders", "equity_history", "balance_cash",
                          "day_start_equity", "day_date", "day_start_initialized",
                          "strategy_stats", "mode_stats", "last_close_time", "events",
                          "mainnet_baseline", "initial_capital"):
                    if k in saved:
                        self.data[k] = saved[k]
                # 同一天重启: 保留今日起始权益 (今日盈亏跨重启连续, 不归零)
                if saved.get("day_date") == utc_today_str() and saved.get("day_start_initialized"):
                    self.data["day_start_initialized"] = True
                # 跨天重启: 今日起始由 check_day_rollover 重新初始化
                if len(self.data["events"]) > 200:
                    self.data["events"] = self.data["events"][-200:]
                self.log.info("已从 %s 恢复状态 (%d 持仓, %d 笔历史成交, %d 条日志)",
                              STATE_FILE, len(self.data["positions"]), len(self.data["trades"]),
                              len(self.data["events"]))
            except Exception as e:
                self.log.warning("状态恢复失败: %s", e)

    def save(self) -> None:
        try:
            tmp = self.state_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, default=str)
            tmp.replace(self.state_file)
        except Exception as e:
            self.log.warning("状态保存失败: %s", e)

    # ---------------- 操作日志 ----------------
    def add_event(self, event_type: str, message: str) -> None:
        """记录一条操作日志. event_type: system/trade/risk/tuning/info/error"""
        with self.lock:
            self.data["events"].append({
                "ts": time.time(),
                "type": event_type,
                "msg": message,
            })
            if len(self.data["events"]) > 200:
                self.data["events"] = self.data["events"][-200:]

    def clear_events(self) -> int:
        """清空操作日志 (清屏用). 返回被清掉的条数, 并落盘 state.json."""
        with self.lock:
            n = len(self.data["events"])
            self.data["events"] = []
        self.save()
        self.log.info("操作日志已清空 (移除 %d 条)", n)
        return n

    # ---------------- 成交流水 (下单/卖出) ----------------
    def record_order(self, order: dict) -> None:
        """记录一笔成交 (action: OPEN=下单 / CLOSE=卖出)"""
        with self.lock:
            self.data.setdefault("orders", []).append(order)
            if len(self.data["orders"]) > 500:
                self.data["orders"] = self.data["orders"][-500:]

    # ---------------- 每日重置 / 熔断 ----------------
    def check_day_rollover(self) -> None:
        today = utc_today_str()
        with self.lock:
            if self.data["day_date"] != today:
                equity = self.equity()
                self.log.info("跨日重置: 今日起始权益 %.2f -> %.2f", self.data["day_start_equity"], equity)
                self.add_event("info", f"📅 跨日重置 | 今日起始权益设为 {equity:.2f} U")
                self.data["day_date"] = today
                self.data["day_start_equity"] = equity
                self.data["day_start_initialized"] = True
                self.data["halted"] = False
                self.data["halt_reason"] = None
                self.save()

    def halt(self, reason: str) -> None:
        with self.lock:
            self.data["halted"] = True
            self.data["halt_reason"] = reason
        self.log.warning("!!!! 熔断停机: %s !!!!", reason)
        self.save()

    def reset_day(self) -> None:
        """手动重置: 以当前权益为新起点, 解除熔断"""
        with self.lock:
            equity = self.equity()
            self.data["day_start_equity"] = equity
            self.data["day_date"] = utc_today_str()
            self.data["halted"] = False
            self.data["halt_reason"] = None
        self.log.info("手动重置: 今日起始权益设为 %.2f, 熔断解除", equity)
        self.save()

    # ---------------- 资金/权益 ----------------
    def equity(self) -> float:
        d = self.data
        unrealized = sum(p["upnl"] for p in d["positions"].values())
        return d["balance_cash"] + unrealized

    def day_pnl(self) -> float:
        return self.equity() - self.data["day_start_equity"]

    def _mode_stats_out(self) -> dict:
        """按模式统计 (带胜率), 供多模式对比面板"""
        out = {}
        for mode, ms in self.data.get("mode_stats", {}).items():
            wr = ms["wins"] / ms["total_trades"] * 100 if ms["total_trades"] else 0.0
            out[mode] = {
                "total_trades": ms["total_trades"],
                "wins": ms["wins"],
                "win_rate": round(wr, 1),
                "realized_pnl": round(ms["realized_pnl"], 2),
                "fees_paid": round(ms["fees_paid"], 2),
            }
        return out

    def exposure(self) -> float:
        """全部持仓名义价值"""
        return sum(p["notional"] for p in self.data["positions"].values())

    def set_balance_from_exchange(self, wallet_balance: float, unrealized: float) -> None:
        with self.lock:
            self.data["balance_cash"] = float(wallet_balance)
            for p in self.data["positions"].values():
                p["upnl"] = unrealized  # 整体未实现, 简化处理

    def set_initial_capital(self, value: float) -> None:
        """设置起始资金 (用户通过设置面板录入, 用于盈亏模块计算盈亏比例)"""
        v = float(value)
        if v <= 0:
            raise ValueError(f"起始资金必须大于 0, 当前 {v}")
        with self.lock:
            self.data["initial_capital"] = round(v, 2)
        self.save()
        self.log.info("起始资金已更新: %.2f", v)

    # ---------------- 持仓 ----------------
    def open_position(self, symbol: str, side: str, qty: float, entry: float,
                      leverage: int, notional: float, upnl: float,
                      tp: float, sl: float, atr: float, strategy: str,
                      record: bool = True, ts: float | None = None) -> None:
        with self.lock:
            self.data["positions"][symbol] = {
                "side": side,              # LONG / SHORT
                "qty": qty,
                "entry": entry,
                "leverage": leverage,
                "notional": notional,
                "upnl": upnl,
                "tp": tp,
                "sl": sl,
                "atr": atr,
                "strategy": strategy,
                "mode": strategy.split("-")[0] if "-" in strategy else strategy,
                "opened_at": time.time(),
                "high": entry,             # 移动止损: 持仓期间最高价 (从入场价起算)
                "low": entry,              # 移动止损: 持仓期间最低价
            }
            # paper 模式锁定保证金
            if self.data["mode"] == "paper":
                margin = notional / leverage
                self.data["balance_cash"] -= margin
            # 成交流水: 下单记录 (交易所同步补录不记, 由交易所回填负责真实历史)
            if record:
                self.record_order({
                    "ts": ts or time.time(),
                    "symbol": symbol,
                    "action": "OPEN",       # 下单(开仓)
                    "side": side,
                    "qty": qty,
                    "price": entry,
                    "leverage": leverage,
                    "notional": notional,
                    "fees": None,
                    "pnl": None,
                    "reason": strategy,
                })

    def close_position(self, symbol: str, exit_price: float, reason: str,
                       record: bool = True, ts: float | None = None) -> dict | None:
        """平仓并结算, 返回成交记录 (paper 模式); live 模式仅记录"""
        with self.lock:
            pos = self.data["positions"].pop(symbol, None)
            if not pos:
                return None
            mode = self.data["mode"]
            pos_mode = pos.get("mode") or pos.get("strategy") or "donchian"
            dir_sign = 1 if pos["side"] == "LONG" else -1
            fee_rate = float(self.cfg["fees"]["taker"])
            gross = (exit_price - pos["entry"]) * pos["qty"] * dir_sign
            fees = (pos["entry"] * pos["qty"] + exit_price * pos["qty"]) * fee_rate
            pnl = gross - fees
            pnl_pct = pnl / pos["notional"] * 100 if pos["notional"] else 0.0

            if mode == "paper":
                margin = pos["notional"] / pos["leverage"]
                self.data["balance_cash"] += margin + pnl

            trade = {
                "ts": time.time(),
                "symbol": symbol,
                "side": pos["side"],
                "qty": pos["qty"],
                "entry": pos["entry"],
                "exit": exit_price,
                "leverage": pos["leverage"],
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "fees": fees,
                "reason": reason,
                "strategy": pos["strategy"],
                "mode": pos_mode,
            }
            self.data["trades"].append(trade)
            self.data["trades"] = self.data["trades"][-200:]
            self.data["last_close_time"][symbol] = time.time()
            # 冷静期开始记录 (持有锁内直接写 events, 避免 add_event 再次加锁死锁)
            cd_min = int(self.cfg["risk"].get("cooldown_minutes", 0) or 0)
            if cd_min > 0:
                self.data["events"].append({
                    "ts": time.time(), "type": "info",
                    "msg": f"⏳ {symbol} 平仓完成, 进入 {cd_min} 分钟冷静期 (可再次开仓前倒计时)",
                })
                if len(self.data["events"]) > 200:
                    self.data["events"] = self.data["events"][-200:]

            st = self.data["strategy_stats"]
            st["total_trades"] += 1
            if pnl > 0:
                st["wins"] += 1
            st["realized_pnl"] += pnl
            st["fees_paid"] += fees

            # 按模式统计 (多模式对比/学习)
            ms = self.data.setdefault("mode_stats", {}).setdefault(pos_mode, {
                "total_trades": 0, "wins": 0, "realized_pnl": 0.0, "fees_paid": 0.0,
            })
            ms["total_trades"] += 1
            if pnl > 0:
                ms["wins"] += 1
            ms["realized_pnl"] += pnl
            ms["fees_paid"] += fees

            # 成交流水: 卖出记录
            if record:
                self.record_order({
                    "ts": ts or time.time(),
                    "symbol": symbol,
                    "action": "CLOSE",      # 卖出(平仓)
                    "side": pos["side"],
                    "qty": pos["qty"],
                    "price": exit_price,
                    "leverage": pos["leverage"],
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "fees": fees,
                    "reason": reason,
                    "strategy": pos["strategy"],
                })
            return trade

    # ---------------- 快照 (给 Web 用) ----------------
    def snapshot(self) -> dict:
        with self.lock:
            d = self.data
            st = d["strategy_stats"]
            equity = self.equity()
            day_pnl = equity - d["day_start_equity"]
            day_pnl_pct = day_pnl / d["day_start_equity"] * 100 if d["day_start_equity"] else 0.0
            win_rate = st["wins"] / st["total_trades"] * 100 if st["total_trades"] else 0.0
            return {
                "mode": d["mode"],
                "running": d["running"],
                "paused": d["paused"],
                "halted": d["halted"],
                "halt_reason": d["halt_reason"],
                "equity": round(equity, 2),
                "balance_cash": round(d["balance_cash"], 2),
                "unrealized": round(equity - d["balance_cash"], 2),
                "day_start_equity": round(d["day_start_equity"], 2),
                "day_pnl": round(day_pnl, 2),
                "day_pnl_pct": round(day_pnl_pct, 2),
                "exposure": round(self.exposure(), 2),
                "positions": [
                    {
                        **p,
                        "symbol": symbol,
                        "mark": d["mark_prices"].get(symbol, d["prices"].get(symbol, p["entry"])),
                    }
                    for symbol, p in d["positions"].items()
                ],
                "trades": list(reversed(d["trades"][-30:])),
                "orders": list(reversed(d.get("orders", [])[-50:])),  # 下单/卖出流水 (最新在前)
                "equity_history": d["equity_history"][-500:],
                "prices": dict(d["prices"]),
                "change_24h": dict(d["change_24h"]),
                "signals": dict(d["signals"]),
                "strategy_stats": {
                    "total_trades": st["total_trades"],
                    "wins": st["wins"],
                    "win_rate": round(win_rate, 1),
                    "realized_pnl": round(st["realized_pnl"], 2),
                    "fees_paid": round(st["fees_paid"], 2),
                },
                "mode_stats": self._mode_stats_out(),
                "risk": {
                    "max_single": self.cfg["risk"]["max_single_order_notional"],
                    "max_total": self.cfg["risk"]["max_total_position_notional"],
                    "daily_loss_stop": self.cfg["risk"]["daily_loss_stop"],
                    "max_positions": self.cfg["risk"]["max_positions"],
                },
                "cooldown_minutes": int(self.cfg["risk"].get("cooldown_minutes", 0) or 0),
                "last_close_time": dict(d["last_close_time"]),   # 供前端实时计算冷静期倒计时
                "events": list(reversed(d["events"][-50:])),   # 操作日志 (最新在前)
                "last_tick_ts": d["last_tick_ts"],             # 主循环真实心跳时间
                "last_update": time.time(),
            }
