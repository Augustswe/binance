"""主交易循环: 行情监控 + 止盈止损 + 熔断 + 策略信号执行 + 通知/定时调参"""
from __future__ import annotations

import asyncio
import time

from core.exchange import BinanceFutures
from core.learner import default_combo, learn, load_current_combo
from core.logger import get_logger
from core.notify import Notifier
from core.orders import rebuild_from_user_trades
from core.risk import RiskManager
from core.state import TradingState
from core.tuner import run_tuning, save_tuned_params
from strategies.donchian import DonchianEngine
from strategies.engine import StrategyEngine
from .execution import LiveExecution, PaperExecution

TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "2h": 7200,
                     "4h": 14400, "8h": 28800, "12h": 43200, "1d": 86400}


class TradingEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.log = get_logger("trader")
        self.state = TradingState(cfg)
        self.exchange = BinanceFutures(cfg)
        self.risk = RiskManager(cfg)
        self.strategy_engine = StrategyEngine(cfg)
        # 策略模式: multi=多策略自适应评分 | donchian=通道突破趋势跟踪
        self.strategy_mode = cfg.get("strategy_mode", "multi")
        self.donchian = DonchianEngine(cfg) if self.strategy_mode == "donchian" else None
        # 自动学习进化器: 加载上次学习到的最优组合, 启动后立即学一轮, 之后每天学
        self.timeframe = cfg["timeframe"]   # 先取配置默认, 学习器组合会覆盖
        lcfg = cfg.get("learner", {})
        self.learner_enabled = bool(lcfg.get("enabled", True)) and self.strategy_mode == "donchian"
        self.learner_days = int(lcfg.get("days", 90))
        self.learner_interval = float(lcfg.get("interval_hours", 24)) * 3600
        if self.strategy_mode == "donchian":
            combo = load_current_combo() or default_combo()
            self._apply_combo(combo, announce=False)   # 覆盖 self.timeframe/_tf_seconds
        self._last_learn_ts = 0.0  # 启动后立即学一轮
        self.execution = (LiveExecution if cfg["mode"] == "live" else PaperExecution)(
            cfg, self.state, self.exchange
        )
        self.symbols = cfg["symbols"]
        self.poll_interval = float(cfg.get("poll_interval", 5))
        self.kline_limit = int(cfg.get("kline_limit", 300))
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_24h_ts = 0.0
        self._last_save_ts = 0.0
        self._last_cycle: dict[str, float] = {}
        # API 登录状态: None=未验证 / True=已验证通过 / False=验证失败
        self._api_verified: bool | None = None
        self._api_wallet: float | None = None

        # Telegram 通知
        tcfg = cfg.get("telegram", {})
        self.notifier = Notifier(
            tcfg.get("bot_token", ""), tcfg.get("chat_id", ""), tcfg.get("enabled", True)
        )
        # AI 自动调参 (仅 multi 模式适用; donchian 模式禁用)
        tun = cfg.get("tuning", {})
        self.tuning_enabled = bool(tun.get("enabled", False)) and self.strategy_mode == "multi"
        self.tuning_interval = float(tun.get("interval_hours", 6)) * 3600
        self.tuning_cfg = tun
        self._last_tune_ts = time.time()  # 启动后先跑一个周期再调参

    # ---------------- 生命周期 ----------------
    async def start(self):
        ok = await asyncio.to_thread(self.exchange.ping)
        if not ok:
            self.log.warning("测试网连接异常, 继续尝试 (行情接口可能不可用)")
        if self.cfg["mode"] == "live":
            self._verify_api()
            await asyncio.to_thread(self._sync_live_positions)
            await asyncio.to_thread(self._backfill_orders_from_exchange)
        self._task = asyncio.create_task(self._loop())
        self.log.info("交易引擎启动: 模式=%s 周期=%s 交易对=%s",
                      self.cfg["mode"], self.timeframe, self.symbols)
        self.state.add_event(
            "system",
            f"🚀 系统启动 | 模式 {'LIVE 测试网' if self.cfg['mode'] == 'live' else 'PAPER 模拟'} | "
            f"周期 {self.timeframe} | 交易对 {', '.join(self.symbols)}",
        )
        await self._notify(
            f"🟢 量化系统启动\n"
            f"模式: {'LIVE 测试网' if self.cfg['mode'] == 'live' else 'PAPER 模拟'}\n"
            f"周期: {self.timeframe} | 交易对: {', '.join(self.symbols)}"
        )

    async def stop(self):
        self._stop.set()
        if self._task:
            try:
                await self._task
            except Exception:
                pass
        self.state.save()

    def control(self, action: str) -> dict:
        d = self.state.data
        if action == "pause":
            d["paused"] = True
            self.state.add_event("info", "⏸ 系统已暂停 (Web 控制)")
            self.log.info("系统暂停 (Web 控制)")
        elif action == "resume":
            d["paused"] = False
            self.state.add_event("info", "▶️ 系统已恢复 (Web 控制)")
            self.log.info("系统恢复 (Web 控制)")
        elif action == "reset_day":
            self.state.reset_day()
            self.state.add_event("info", "🔄 手动重置今日: 以当前权益为新起点")
        else:
            return {"ok": False, "msg": f"未知操作: {action}"}
        return {"ok": True, "msg": action}

    def get_snapshot(self) -> dict:
        return self.state.snapshot()

    # ---------------- Web 设置面板: 热更新 ----------------
    def update_symbols(self, symbols: list[str]) -> dict:
        """热更新交易对列表 (Web 设置面板): 写 config.yaml + 更新内存

        - 已有持仓的交易对禁止移除 (需先平仓)
        - 更新后立即生效, 重启后依然保留
        """
        symbols = [s.strip().upper() for s in symbols if s.strip()]
        # 去重保序
        seen, dedup = set(), []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                dedup.append(s)
        symbols = dedup

        old = set(self.symbols)
        new = set(symbols)
        removed = old - new
        positions = set(self.state.data["positions"].keys())
        blocked = removed & positions
        if blocked:
            return {
                "ok": False,
                "msg": f"❌ 以下交易对仍有持仓, 无法移除: {', '.join(sorted(blocked))} (请先平仓)",
            }

        from core.config import update_config_symbols

        try:
            update_config_symbols(symbols)
        except Exception as e:
            return {"ok": False, "msg": f"❌ 写入 config.yaml 失败: {e}"}

        self.symbols = symbols
        self.cfg["symbols"] = symbols
        added = new - old
        self.state.add_event(
            "info",
            f"⚙️ 交易对已更新: {'+'.join(sorted(added)) or '无新增'} "
            f"{'/-'.join(sorted(removed)) or ''} | 共{len(symbols)}个",
        )
        self.state.save()
        self.log.info("交易对更新: %s", symbols)
        return {"ok": True, "symbols": symbols, "added": sorted(added), "removed": sorted(removed)}

    def update_api(self, key: str, secret: str) -> dict:
        """热更新测试网 API Key/Secret (Web 设置面板): 写 .env + 更新内存

        live 模式下用新凭据立即验证一次账户接口
        """
        key, secret = key.strip(), secret.strip()
        from core.config import update_env_api

        try:
            update_env_api(key, secret)
        except Exception as e:
            return {"ok": False, "msg": f"❌ 写入 .env 失败: {e}"}

        self.exchange.set_credentials(key, secret)
        self.cfg["api"]["key"] = key
        self.cfg["api"]["secret"] = secret

        if self.cfg["mode"] == "live":
            try:
                acc = self.exchange.get_account()
                wallet = float(acc.get("totalWalletBalance", 0.0))
                self._api_verified = True
                self._api_wallet = wallet
                self.state.data["balance_cash"] = wallet
                self.state.add_event(
                    "info",
                    f"🔑 API 已更新并通过验证 | 钱包余额 {wallet:.2f} U",
                )
                return {"ok": True, "msg": f"✅ API 已更新并通过验证, 钱包余额 {wallet:.2f} U"}
            except Exception as e:
                self._api_verified = False
                self._api_wallet = None
                self.state.add_event("error", f"⚠️ API 已写入但验证失败: {str(e)[:80]}")
                return {"ok": True, "msg": f"⚠️ API 已写入 .env, 但验证失败: {str(e)[:80]}"}

        self._api_verified = None
        self.state.add_event("info", "🔑 API 已更新 (paper 模式无需验证)")
        return {"ok": True, "msg": "✅ API 已保存"}

    def _verify_api(self) -> None:
        """启动时验证 API 登录状态 (live 模式)"""
        if self.cfg["mode"] != "live":
            self._api_verified = None
            return
        if not self.cfg["api"].get("key"):
            self._api_verified = False
            self._api_wallet = None
            self.log.warning("live 模式未配置 API Key, 无法登录测试网")
            return
        try:
            acc = self.exchange.get_account()
            self._api_verified = True
            self._api_wallet = float(acc.get("totalWalletBalance", 0.0))
            self.log.info("API 登录验证通过, 钱包余额 %.2f U", self._api_wallet)
        except Exception as e:
            self._api_verified = False
            self._api_wallet = None
            self.log.error("API 登录验证失败: %s", str(e)[:100])

    def _estimate_today_start_equity(self) -> float | None:
        """估算今天(本地时区)0点的权益 (交易所 income 反推, 只用于 live 模式)

        今日0点权益 ≈ 当前钱包余额 - 今日已发生资金流水(盈亏/手续费/资金费)
        这样今日盈亏从本地0点算起, 系统当天中途启动/重启也不会"重新计算"
        """
        if self.cfg["mode"] != "live":
            return None
        try:
            import datetime

            now_local = datetime.datetime.now()
            t0 = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            t0_ms = int(t0.timestamp() * 1000)   # 本地0点 → UTC 毫秒
            acc = self.exchange.get_account()
            wallet = float(acc.get("totalWalletBalance", 0.0))
            incomes = self.exchange.get_income(start_ms=t0_ms)
            # 今日资金流水合计 (排除入金/出金, 它不算盈亏)
            today_flow = sum(
                float(i.get("income", 0))
                for i in incomes
                if i.get("incomeType") not in ("TRANSFER", "DEPOSIT", "WITHDRAW")
            )
            est = wallet - today_flow
            return round(est, 2) if est > 0 else None
        except Exception as e:
            self.log.warning("估算今日0点权益失败: %s", str(e)[:80])
            return None

    def api_status(self) -> dict:
        """API 登录状态 (供 Web 展示): verified True=已登录 / False=未登录 / None=无需"""
        key = self.cfg["api"].get("key", "")
        return {
            "mode": self.cfg["mode"],
            "configured": bool(key),
            "verified": self._api_verified,
            "wallet": self._api_wallet,
            "key_masked": (key[:6] + "****" + key[-4:]) if len(key) > 12 else (key[:4] + "****" if key else ""),
        }

    # ---------------- 主循环 ----------------
    async def _loop(self):
        while not self._stop.is_set():
            try:
                await self._tick()
                await self._maybe_tune()
                await self._maybe_learn()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.log.exception("tick 异常: %s", e)
            await asyncio.sleep(self.poll_interval)

    async def _tick(self):
        self.state.check_day_rollover()
        d = self.state.data
        if not d["running"]:
            return

        # 1) 行情
        try:
            prices = await asyncio.to_thread(self.exchange.get_all_prices)
            mark = await asyncio.to_thread(self.exchange.get_mark_prices)
        except Exception as e:
            self.log.warning("行情获取失败: %s", e)
            self.state.add_event("error", f"⚠️ 行情获取失败: {str(e)[:60]}")
            return
        now = time.time()
        d["last_tick_ts"] = now  # 心跳: 主循环真实活动时间
        if now - self._last_24h_ts > 60:
            try:
                t24 = await asyncio.to_thread(self.exchange.get_all_24hr)
                d["change_24h"] = {
                    s: t24.get(s, {}).get("change_pct", 0.0) for s in self.symbols if s in t24
                }
                self._last_24h_ts = now
            except Exception as e:
                self.log.warning("24小时行情获取失败: %s", e)
        d["prices"] = {s: prices.get(s, d["prices"].get(s, 0.0)) for s in self.symbols}
        d["mark_prices"] = {s: mark.get(s, d["prices"].get(s, 0.0)) for s in self.symbols}

        # 1.5) live 模式: 从交易所同步余额与持仓
        if self.cfg["mode"] == "live":
            try:
                await self._sync_live_account()
            except Exception as e:
                self.log.warning("live 账户同步失败: %s", e)

        # 2) 未实现盈亏 / 名义价值
        for sym, pos in list(d["positions"].items()):
            p = d["mark_prices"].get(sym) or d["prices"].get(sym)
            if not p or p <= 0:
                continue
            sign = 1.0 if pos["side"] == "LONG" else -1.0
            pos["upnl"] = (p - pos["entry"]) * pos["qty"] * sign
            pos["notional"] = pos["qty"] * p

        # 3) 权益曲线
        d["equity_history"].append([now, round(self.state.equity(), 2)])
        if len(d["equity_history"]) > 5000:
            d["equity_history"] = d["equity_history"][-2000:]

        # 3.5) 今日起始权益初始化
        # 优先用"今天 UTC 0 点"的权益 (income 反推), 而不是启动时刻权益:
        # 这样即使系统当天中途启动/重启, 今日盈亏都从 0 点算起, 不会"重新计算"
        if not d.get("day_start_initialized"):
            est = self._estimate_today_start_equity()
            if est is not None and est > 0:
                d["day_start_equity"] = est
            else:
                d["day_start_equity"] = self.state.equity()
            d["day_start_initialized"] = True
            self.log.info("今日起始权益初始化: %.2f (今日0点估算%s)",
                          d["day_start_equity"], "" if est else "[失败,用当前权益]")
            self.state.add_event(
                "info",
                f"📅 今日起始权益: {d['day_start_equity']:.2f} U (从今天0点起算)"
                if est else f"📅 今日起始权益: {d['day_start_equity']:.2f} U (无历史数据,从当前起算)",
            )

        # 4) 止盈止损
        await self._check_tp_sl()

        # 4.5) 兜底巡检: 确保每个持仓在交易所都有止损单 (行情断流也能止损)
        await self._ensure_exchange_stops()

        # 5) 熔断
        if self.risk.should_halt(self.state):
            self.state.add_event("risk", f"🚨 熔断停机: {self.state.data['halt_reason']}")
            await self._notify(
                f"🚨 熔断停机!\n原因: {self.state.data['halt_reason']}\n"
                f"当前权益: {self.state.equity():.2f} U"
            )
            if self.cfg["mode"] == "live":
                await asyncio.to_thread(self.exchange.close_all)
            return

        # 6) 策略周期 (每根K线每个币种最多一次)
        for sym in self.symbols:
            if self._cycle_due(sym):
                await self._strategy_cycle(sym)

        # 7) 定期保存
        if now - self._last_save_ts > 30:
            self.state.save()
            self._last_save_ts = now

    def _cycle_due(self, symbol: str) -> bool:
        last = self._last_cycle.get(symbol, 0.0)
        # donchian 模式: 每5分钟检测一次通道突破, 防止突破发生却被延迟到下一根K线才入场
        interval = 300 if self.strategy_mode == "donchian" else self._tf_seconds
        return (time.time() - last) >= interval

    # ---------------- 止盈止损 ----------------
    async def _check_tp_sl(self):
        d = self.state.data
        tp_atr = float(self.cfg["position"]["tp_atr"])
        sl_atr = float(self.cfg["position"]["sl_atr"])
        for sym, pos in list(d["positions"].items()):
            p = d["mark_prices"].get(sym)
            if not p or p <= 0:
                continue
            # 兜底: 交易所同步/重启恢复的持仓可能没有TP/SL, 用最近ATR补算, 避免裸奔
            # donchian 模式: 只补 SL (ATR止损), 不补 TP (让利润奔跑)
            sig = d["signals"].get(sym) or {}
            atr = sig.get("atr") or 0.0
            sign = 1.0 if pos["side"] == "LONG" else -1.0
            need_sl = not pos.get("sl")
            need_tp = self.strategy_mode != "donchian" and not pos.get("tp")
            if (need_sl or need_tp) and atr and atr > 0:
                if need_tp:
                    pos["tp"] = pos["entry"] + sign * tp_atr * atr
                if need_sl:
                    d_sl = self.donchian.sl_atr if self.strategy_mode == "donchian" else sl_atr
                    pos["sl"] = pos["entry"] - sign * d_sl * atr
                self.log.info("[%s] 补算止损 SL=%.2f", sym, pos["sl"])
                self.state.add_event("info", f"🛡 {sym} 补算止损 SL={pos['sl']:.2f}")
                # 补算后立即把止损下到交易所
                await self._place_exchange_stop(sym, pos)
            if pos["side"] == "LONG":
                if pos.get("tp") and p >= pos["tp"]:
                    trade = await self.execution.close_position(sym, p, "止盈TP")
                    if trade:
                        await self._notify_close(trade)
                elif pos.get("sl") and p <= pos["sl"]:
                    trade = await self.execution.close_position(sym, p, "止损SL")
                    if trade:
                        await self._notify_close(trade)
            else:
                if pos.get("tp") and p <= pos["tp"]:
                    trade = await self.execution.close_position(sym, p, "止盈TP")
                    if trade:
                        await self._notify_close(trade)
                elif pos.get("sl") and p >= pos["sl"]:
                    trade = await self.execution.close_position(sym, p, "止损SL")
                    if trade:
                        await self._notify_close(trade)

    async def _place_exchange_stop(self, sym: str, pos: dict) -> bool:
        """把止损单挂到交易所 (交易所侧自动执行, 行情断流也能止损)"""
        if self.cfg["mode"] != "live":
            return False
        sl = pos.get("sl")
        if not sl or sl <= 0:
            return False
        if pos.get("stop_placed"):
            return True
        stop_side = "SELL" if pos["side"] == "LONG" else "BUY"
        try:
            await asyncio.to_thread(
                self.exchange.place_stop_market, sym, stop_side, sl
            )
            pos["stop_placed"] = True
            self.log.info("[%s] 交易所止损单已挂 %s SL=%.2f", sym, stop_side, sl)
            self.state.add_event("info", f"🎯 {sym} 交易所止损单已挂 ({stop_side} @ {sl:.2f})")
            return True
        except Exception as e:
            self.log.error("[%s] 挂止损单失败: %s", sym, e)
            return False

    async def _ensure_exchange_stops(self):
        """兜底巡检: 每60秒确保每个持仓在交易所都有止损单 (重启/撤单后自动补挂)"""
        if self.cfg["mode"] != "live":
            return
        now = time.time()
        if now - getattr(self, "_last_stop_check", 0) < 60:
            return
        self._last_stop_check = now
        try:
            open_orders = await asyncio.to_thread(self.exchange.get_open_orders)
            covered = set()
            for o in open_orders:
                otype = o.get("type") or o.get("orderType")
                ostatus = o.get("status") or o.get("algoStatus")
                if otype in ("STOP_MARKET",) and ostatus in ("NEW", "WORKING"):
                    trig = float(o.get("triggerPrice") or o.get("stopPrice", 0))
                    covered.add((o.get("symbol"), round(trig, 2)))
        except Exception as e:
            self.log.warning("止损单巡检失败: %s", e)
            return
        for sym, pos in list(self.state.data["positions"].items()):
            sl = pos.get("sl")
            if not sl or sl <= 0:
                continue
            # 交易所已有该价格的止损单则跳过
            if (sym, round(sl, 2)) in covered:
                pos["stop_placed"] = True
                continue
            if not pos.get("stop_placed"):
                await self._place_exchange_stop(sym, pos)

    # ---------------- 策略周期 ----------------
    async def _strategy_cycle(self, symbol: str):
        self._last_cycle[symbol] = time.time()
        try:
            klines = await asyncio.to_thread(
                self.exchange.get_klines, symbol, self.timeframe, self.kline_limit
            )
        except Exception as e:
            self.log.error("[%s] K线获取失败: %s", symbol, e)
            return

        d = self.state.data
        pos = d["positions"].get(symbol)
        mark = d["mark_prices"].get(symbol) or d["prices"].get(symbol)
        if not mark or mark <= 0:
            return

        # ============ Donchian 模式: 通道突破趋势跟踪 ============
        if self.strategy_mode == "donchian":
            sig = self.donchian.analyze(symbol, klines)
            if sig is None:
                return
            d["signals"][symbol] = sig.to_dict()
            if not pos:
                if sig.action == "LONG":
                    await self._try_open_donchian(symbol, "LONG", mark, sig)
                elif sig.action == "SHORT":
                    await self._try_open_donchian(symbol, "SHORT", mark, sig)
            else:
                # 通道反向突破 → 平仓 (让利润奔跑, 无固定止盈)
                if self.donchian.check_exit(klines, pos):
                    trade = await self.execution.close_position(symbol, mark, "通道反向出场")
                    if trade:
                        await self._notify_close(trade)
            return

        # ============ multi 模式: 多策略自适应评分 ============
        result = self.strategy_engine.analyze(symbol, klines)
        if result is None:
            return
        d["signals"][symbol] = result.to_dict()
        open_th = float(self.cfg["signal"]["open_threshold"])
        close_th = float(self.cfg["signal"]["close_threshold"])

        if not pos:
            # trend_only: 只在趋势市顺势开仓 (trend_up只做多, trend_down只做空, 震荡市不开)
            trend_only = bool(self.cfg.get("signal", {}).get("trend_only", False))
            can_long = (not trend_only) or result.regime == "trend_up"
            can_short = (not trend_only) or result.regime == "trend_down"
            if result.combined >= open_th and can_long:
                await self._try_open(symbol, "LONG", mark, result)
            elif result.combined <= -open_th and can_short:
                await self._try_open(symbol, "SHORT", mark, result)
        else:
            # close_threshold <= 0 表示禁用"信号消失"平仓, 只靠止盈止损离场
            if close_th > 0:
                if pos["side"] == "LONG" and result.combined < close_th:
                    trade = await self.execution.close_position(symbol, mark, "信号消失")
                    if trade:
                        await self._notify_close(trade)
                elif pos["side"] == "SHORT" and result.combined > -close_th:
                    trade = await self.execution.close_position(symbol, mark, "信号消失")
                    if trade:
                        await self._notify_close(trade)

    async def _try_open(self, symbol: str, side: str, price: float, result):
        d = self.state.data
        exposure = self.state.exposure()
        budget = min(
            float(self.cfg["risk"]["max_single_order_notional"]),
            float(self.cfg["risk"]["max_total_position_notional"]) - exposure,
        )
        if budget <= 0:
            return
        try:
            min_qty, step = await asyncio.to_thread(self.exchange.lot_size, symbol)
        except Exception as e:
            self.log.error("[%s] 交易规则获取失败: %s", symbol, e)
            return
        qty = self.exchange.round_qty(budget / price, step)
        if qty <= 0 or qty < min_qty:
            self.log.info("[%s] 下单数量 %.6f 低于最小 %.6f, 跳过", symbol, qty, min_qty)
            return
        notional = qty * price
        ok, reason = self.risk.check_open(self.state, symbol, notional)
        if not ok:
            self.log.info("[%s] 开仓被风控拒绝(%s): %s", symbol, side, reason)
            self.state.add_event("info", f"🛡 {symbol} 开仓被风控拒绝({side}): {reason}")
            return
        leverage = result.leverage
        tp_atr = float(self.cfg["position"]["tp_atr"])
        sl_atr = float(self.cfg["position"]["sl_atr"])
        sign = 1.0 if side == "LONG" else -1.0
        # 止盈止损最小距离: 保证至少覆盖 2.5倍/1.5倍 双边手续费, 防止止盈被手续费吃掉
        pos_cfg = self.cfg.get("position", {})
        min_tp = price * float(pos_cfg.get("min_tp_pct", 0.0025))
        min_sl = price * float(pos_cfg.get("min_sl_pct", 0.0015))
        tp = price + sign * max(tp_atr * result.atr, min_tp)
        sl = price - sign * max(sl_atr * result.atr, min_sl)
        ok = await self.execution.open_position(
            symbol, side, qty, price, leverage, notional, tp, sl, result.atr, result.regime
        )
        if ok:
            self.state.add_event(
                "trade",
                f"🟢 开仓 {symbol} {'做多' if side == 'LONG' else '做空'} "
                f"{qty} @ {price:.2f} | 杠杆 {leverage}x | 名义 {notional:.1f}U | "
                f"状态 {result.regime} 评分 {result.combined:.2f}",
            )
            await self._notify(
                f"🟢 开仓 {symbol}\n"
                f"方向: {'做多 LONG' if side == 'LONG' else '做空 SHORT'}\n"
                f"数量: {qty} | 价格: {price:.2f}\n"
                f"杠杆: {leverage}x | 名义: {notional:.1f} U\n"
                f"止盈: {tp:.2f} | 止损: {sl:.2f}\n"
                f"市场状态: {result.regime} | 综合评分: {result.combined:.2f}"
            )

    # ---------------- live 账户同步 ----------------
    async def _sync_live_account(self):
        """从交易所同步余额和持仓 (live 模式每轮调用)"""
        acc = await asyncio.to_thread(self.exchange.get_account)
        wallet = float(acc.get("totalWalletBalance", 0.0))
        self.state.data["balance_cash"] = wallet
        # 每笔持仓的未实现盈亏由 positionRisk 校准 (_reconcile_positions)
        risks = await asyncio.to_thread(self.exchange.get_position_risk)
        self._reconcile_positions(risks)

    def _reconcile_positions(self, risks: list[dict]):
        """按交易所 positionRisk 校准本地持仓"""
        exchange_pos = {}
        for r in risks:
            sym = r.get("symbol")
            if sym not in self.symbols:
                continue
            amt = float(r.get("positionAmt", 0))
            if abs(amt) < 1e-8:
                continue
            exchange_pos[sym] = r
            side = "LONG" if amt > 0 else "SHORT"
            qty = abs(amt)
            entry = float(r.get("entryPrice", 0))
            lev = max(1, int(float(r.get("leverage", 1))))
            upnl = float(r.get("unRealizedProfit", 0.0))
            pos = self.state.data["positions"].get(sym)
            if pos and pos["side"] == side and abs(pos["qty"] - qty) < 1e-8:
                pos["entry"] = entry
                pos["leverage"] = lev
                pos["upnl"] = upnl
                continue
            # 交易所有多、本地没有 (外部开仓/重启) → 补录 (不产生假成交记录, 由交易所回填负责)
            self.state.open_position(
                sym, side, qty, entry, lev, qty * entry, upnl, 0.0, 0.0, 0.0, "交易所同步",
                record=False,
            )
            self.log.info("[live] 同步持仓 %s %s %.4f@%.2f", sym, side, qty, entry)
            self.state.add_event("info", f"🔁 交易所同步持仓 {sym} {'做多' if side == 'LONG' else '做空'} {qty} @ {entry:.2f}")
        # 本地有、交易所没有 → 已平仓 (外部/强平)
        for sym in list(self.state.data["positions"].keys()):
            if sym not in exchange_pos:
                mark = self.state.data["mark_prices"].get(sym, 0.0)
                self.state.close_position(sym, mark or self.state.data["prices"].get(sym, 0.0), "外部平仓", record=False)

    def _backfill_orders_from_exchange(self):
        """启动时从交易所 userTrades 回填权威成交记录 (下单/卖出流水 + 平仓明细 + 统计)"""
        try:
            orders, trades, stats = rebuild_from_user_trades(self.exchange, self.symbols)
        except Exception as e:
            self.log.error("回填成交记录失败: %s", e)
            return
        if not orders:
            self.log.info("交易所暂无成交记录可回填")
            return
        # 保留系统运行期间已经记录的本地订单, 与交易所权威记录合并去重
        local_orders = [o for o in self.state.data.get("orders", []) if o.get("action") in ("OPEN", "CLOSE")]
        merged: dict[tuple, dict] = {}
        for o in orders:
            merged[self._order_key(o)] = dict(o)
        for o in local_orders:
            key = self._order_key(o)
            if key in merged:
                # 本地保留策略/原因, 手续费与盈亏以交易所为准
                for f in ("strategy", "reason"):
                    if o.get(f):
                        merged[key][f] = o[f]
                if merged[key].get("fees") is None and o.get("fees") is not None:
                    merged[key]["fees"] = o["fees"]
                if merged[key].get("pnl") is None and o.get("pnl") is not None:
                    merged[key]["pnl"] = o["pnl"]
            else:
                merged[key] = dict(o)
        merged_orders = sorted(merged.values(), key=lambda o: o["ts"])
        self.state.data["orders"] = merged_orders[-500:]
        # 平仓明细与统计用交易所权威值 (覆盖本地估算)
        if trades:
            self.state.data["trades"] = trades[-200:]
            self.state.data["strategy_stats"] = stats
        self.state.save()
        self.log.info("已从交易所回填成交记录: %d 笔 (下单/卖出), %d 笔平仓, 已实现 %.2fU, 手续费 %.2fU",
                      len(merged_orders), len(trades), stats["realized_pnl"], stats["fees_paid"])
        self.state.add_event(
            "info",
            f"📊 已从交易所回填成交记录: 流水 {len(merged_orders)} 笔 | 平仓 {len(trades)} 笔 | "
            f"已实现 {stats['realized_pnl']:+.2f}U | 手续费 {stats['fees_paid']:.2f}U",
        )

    @staticmethod
    def _order_key(o: dict) -> tuple:
        """成交流水去重键: 时间窗口 + 币种 + 方向 + 数量 + 价格"""
        return (
            int(o.get("ts", 0) // 60),
            o.get("symbol"),
            o.get("action"),
            o.get("side"),
            round(float(o.get("qty", 0)), 8),
            round(float(o.get("price", 0)), 6),
        )

    def _sync_live_positions(self):
        try:
            risks = self.exchange.get_position_risk()
        except Exception as e:
            self.log.error("同步持仓失败: %s", e)
            return
        self._reconcile_positions(risks)

    # ---------------- 通知 ----------------
    async def _notify(self, text: str):
        try:
            await asyncio.to_thread(self.notifier.send, text)
        except Exception as e:
            self.log.warning("通知发送异常: %s", e)

    async def _notify_close(self, trade: dict):
        emoji = "🟢" if trade["pnl"] >= 0 else "🔻"
        self.state.add_event(
            "trade",
            f"{emoji} 平仓 {trade['symbol']} "
            f"{'做多' if trade['side'] == 'LONG' else '做空'} "
            f"{trade['qty']} @ {trade['entry']:.2f} -> {trade['exit']:.2f} | "
            f"盈亏 {trade['pnl']:+.2f}U ({trade['pnl_pct']:+.2f}%) | {trade['reason']}",
        )
        await self._notify(
            f"{emoji} 平仓 {trade['symbol']}\n"
            f"方向: {'做多' if trade['side'] == 'LONG' else '做空'}\n"
            f"数量: {trade['qty']} | 入场 {trade['entry']:.2f} -> 出场 {trade['exit']:.2f}\n"
            f"盈亏: {trade['pnl']:+.2f} U ({trade['pnl_pct']:+.2f}%)\n"
            f"原因: {trade['reason']}\n"
            f"今日权益: {self.state.equity():.2f} U"
        )

    # ---------------- AI 定时调参 ----------------
    async def _maybe_tune(self):
        if not self.tuning_enabled:
            return
        now = time.time()
        if now - self._last_tune_ts < self.tuning_interval:
            return
        self._last_tune_ts = now
        self.log.info("===== 开始 AI 自动调参 =====")
        try:
            result = await asyncio.to_thread(
                run_tuning, self.cfg,
                symbol=self.tuning_cfg.get("symbol", "BTCUSDT"),
                timeframe=self.tuning_cfg.get("timeframe", "5m"),
                bars=int(self.tuning_cfg.get("bars", 2000)),
                trials=int(self.tuning_cfg.get("trials", 20)),
            )
        except Exception as e:
            self.log.error("AI 调参失败: %s", e)
            return
        if result.get("error"):
            self.log.error("AI 调参失败: %s", result["error"])
            self.state.add_event("error", f"⚠️ AI 调参失败: {result['error']}")
            return
        if result.get("improved"):
            path = save_tuned_params(result)
            # 应用到内存中的配置
            self._apply_tuned(result["params"])
            self.log.info("AI 调参已应用: %s", path)
            self.state.add_event(
                "tuning",
                f"🤖 AI 自动调参完成 | 评分 {result['base_score']} -> {result['score']} | "
                f"基线收益 {result['base_stats']['total_return_pct']}% "
                f"Sharpe {result['base_stats']['sharpe']} | 已应用新参数",
            )
            await self._notify(
                f"🤖 AI 自动调参完成\n"
                f"基线评分 {result['base_score']} -> {result['score']}\n"
                f"基线: 收益{result['base_stats']['total_return_pct']}% "
                f"Sharpe {result['base_stats']['sharpe']}\n"
                f"新参数: {result['params']}"
            )
        else:
            self.log.info("AI 调参完成, 当前参数已较优, 不调整")
            self.state.add_event(
                "tuning",
                f"🤖 AI 调参完成 | 评分 {result['base_score']} -> {result['score']} | 当前参数已较优, 不调整",
            )

    def _apply_tuned(self, params: dict):
        """把优化参数应用到运行中的配置并重建策略引擎"""
        import copy

        new_cfg = copy.deepcopy(self.cfg)
        from core.tuner import _apply_params

        _apply_params(new_cfg, params)
        # 只更新策略相关段落
        for section in ("signal", "position", "strategies"):
            if section in new_cfg:
                self.cfg[section] = new_cfg[section]
        self.strategy_engine = StrategyEngine(self.cfg)
        self.log.info("新策略参数已生效: signal=%s position=%s", self.cfg["signal"], self.cfg["position"])

    async def _try_open_donchian(self, symbol: str, side: str, price: float, sig):
        """Donchian 模式开仓: 只设 ATR 止损, 无固定止盈 (让利润奔跑, 通道反向出场)

        动态杠杆: 强信号(突破深)高倍开单 → 名义仓位大; 弱信号低倍试错 → 小仓位
        保证金预算固定, 名义价值 = 保证金 × 杠杆
        """
        d = self.state.data
        exposure = self.state.exposure()
        leverage = max(1, int(getattr(sig, "leverage", 1)))
        lev_max = getattr(self.donchian, "lev_max", 5) if self.donchian else 5
        max_single = float(self.cfg["risk"]["max_single_order_notional"])
        margin_base = float(self.cfg["risk"].get(
            "margin_per_position", max_single / max(1, lev_max)
        ))
        # 名义价值 = 保证金 × 杠杆 (受单笔上限与总敞口上限约束)
        budget = min(margin_base * leverage, max_single,
                     float(self.cfg["risk"]["max_total_position_notional"]) - exposure)
        if budget <= 0:
            return
        try:
            min_qty, step = await asyncio.to_thread(self.exchange.lot_size, symbol)
        except Exception as e:
            self.log.error("[%s] 交易规则获取失败: %s", symbol, e)
            return
        qty = self.exchange.round_qty(budget / price, step)
        if qty <= 0 or qty < min_qty:
            return
        notional = qty * price
        ok, reason = self.risk.check_open(self.state, symbol, notional)
        if not ok:
            self.state.add_event("info", f"🛡 {symbol} 开仓被风控拒绝({side}): {reason}")
            return
        sl = sig.sl if side == "LONG" else sig.sl
        ok = await self.execution.open_position(
            symbol, side, qty, price, leverage, notional, None, sl, sig.atr, f"donchian-{sig.regime}"
        )
        if ok:
            self.state.add_event(
                "trade",
                f"🟢 Donchian开仓 {symbol} {'做多' if side == 'LONG' else '做空'} "
                f"{qty} @ {price:.2f} | 杠杆 {leverage}x | 强度 {sig.strength:.2f} | "
                f"突破通道 | 止损 {sl:.2f}",
            )
            await self._notify(
                f"🟢 Donchian 开仓 {symbol}\n"
                f"方向: {'做多' if side == 'LONG' else '做空'}\n"
                f"数量: {qty} | 价格: {price:.2f}\n"
                f"杠杆: {leverage}x (信号强度 {sig.strength:.2f})\n"
                f"止损: {sl:.2f} (ATR)\n"
                f"出场: 通道反向突破, 无固定止盈"
            )

    # ---------------- 自动学习进化 ----------------
    def _apply_combo(self, combo: dict, announce: bool = True):
        """应用策略组合: 更新周期与 Donchian 参数"""
        key = f"{combo['timeframe']}-{combo['entry_n']}/{combo['exit_n']}/{combo['sl_atr']}"
        old_key = getattr(self, "_current_combo_key", None)
        self._current_combo_key = key
        self.timeframe = combo["timeframe"]
        self._tf_seconds = TIMEFRAME_SECONDS.get(self.timeframe, 86400)
        if self.donchian:
            self.donchian.set_params(combo)
        self.log.info("当前策略组合: %s", key)
        if announce and old_key != key:
            self.state.add_event("tuning", f"🤖 自动学习切换策略: {old_key} → {key}")
        self.state.save()

    async def _maybe_learn(self):
        """自动学习: 启动后立即学一轮, 之后每 learner_interval 秒学一轮"""
        if not self.learner_enabled:
            return
        now = time.time()
        if now - self._last_learn_ts < self.learner_interval:
            return
        self._last_learn_ts = now
        self.log.info("===== 开始自动学习进化 (最近%d天, 策略池评估) =====", self.learner_days)
        try:
            result = await asyncio.to_thread(learn, self.cfg, days=self.learner_days)
        except Exception as e:
            self.log.error("自动学习失败: %s", e)
            self.state.add_event("error", f"⚠️ 自动学习失败: {str(e)[:50]}")
            return
        if result.get("error"):
            self.log.error("自动学习失败: %s", result["error"])
            self.state.add_event("error", f"⚠️ 自动学习失败: {result['error']}")
            return
        best = result["best"]
        rankings = result["rankings"]
        rank_str = " | ".join(f"{r['key']}({r['score']})" for r in rankings[:4])
        if best["key"] != self._current_combo_key:
            old = self._current_combo_key
            self._apply_combo(best["combo"])
            self.state.add_event(
                "tuning",
                f"🤖 自动学习进化: {old} → {best['key']} | "
                f"近{self.learner_days}天收益{best['avg_ret']:+.2f}% 回撤{best['avg_dd']:.2f}% | 排名: {rank_str}",
            )
            await self._notify(
                f"🤖 自动学习: 策略已进化\n"
                f"旧组合: {old}\n新组合: {best['key']}\n"
                f"近{self.learner_days}天回测: 收益{best['avg_ret']:+.2f}% 回撤{best['avg_dd']:.2f}%\n"
                f"排名: {rank_str}"
            )
        else:
            self.state.add_event(
                "tuning",
                f"🤖 自动学习完成: 保持 {best['key']} | "
                f"近{self.learner_days}天收益{best['avg_ret']:+.2f}% 回撤{best['avg_dd']:.2f}%",
            )
            self.log.info("自动学习: 保持最优组合 %s (评分%.2f)", best["key"], best["score"])
