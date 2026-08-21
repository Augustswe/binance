"""主交易循环: 行情监控 + 止盈止损 + 熔断 + 策略信号执行 + 通知/定时调参"""
from __future__ import annotations

import asyncio
import math
import time

from core.exchange import BinanceFutures
from core.learner import (default_combo, learn, learn_mode_weights,
                          load_current_combo, load_mode_weights)
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

# 主网分级解锁: 测试网跑满对应天数, 主网才开放并提高限额 (保护真实资金)
# 阈值(天) 与 对应档位限额(U); 跑满90天后进入 t3, 限额改为用户自定义
# ⚠️ 安全护栏: 请勿为"提前解锁"而移除/篡改本逻辑或回填 mainnet.since。
#    开源可改, 但解除该限制等于直接暴露全额真实资金风险, 后果自负。
MAINNET_TIER_DAYS = (30, 60, 90)
MAINNET_TIER_CAPS = (500.0, 1000.0)


def _resolve_manual(entry: float, sign: float, spec: dict, is_tp: bool) -> float:
    """把手动止盈止损规格解析成绝对价格, 供 _check_tp_sl / API 共用。

    spec: {"type": "price"|"pct", "value": <number>}
    sign = +1 (LONG) / -1 (SHORT)
    TP 永远在入场价顺向(盈利方向), SL 逆向(亏损方向)。
      price: 直接用 value
      pct  : 相对入场价的百分比(value 为正数, 如 5 表示 +5%)
    """
    t = (spec or {}).get("type")
    v = float((spec or {}).get("value", 0) or 0)
    if t == "price":
        return float(v)
    # pct
    if is_tp:
        return entry * (1.0 + sign * v / 100.0)
    return entry * (1.0 - sign * v / 100.0)


class TradingEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.log = get_logger("trader")
        self.state = TradingState(cfg)
        self.exchange = BinanceFutures(cfg)
        self.risk = RiskManager(cfg)
        self.strategy_engine = StrategyEngine(cfg)
        # 策略模式: 支持多模式并行 (donchian/multi/grid/ma_cross/rsi/bollinger)
        self.strategy_mode = cfg.get("strategy_mode", "multi")
        from strategies.modes import ModeManager

        self.modes = ModeManager(cfg)
        self.enabled_modes = self.modes.enabled
        self.donchian = self.modes.donchian  # 兼容旧代码引用
        # 加载模式学习器学到的资金权重 (按实盘表现分配)
        learned_w = load_mode_weights()
        if learned_w:
            self.modes.update_weights(learned_w)
        # ML 门禁 / 选择器: 加载离线训练好的模型 (缺失则安全降级为关闭, 不影响原逻辑)
        mlcfg = cfg.get("ml_filter", {})
        self.ml_enabled = bool(mlcfg.get("enabled", False))
        self.ml_gate = bool(mlcfg.get("gate", True)) and self.ml_enabled
        self.ml_selector = bool(mlcfg.get("selector", True)) and self.ml_enabled
        self.ml_threshold = float(mlcfg.get("threshold", 0.55))
        self.ml_model_file = mlcfg.get("model_file", "data/ml_filter.pkl")
        self.ml = None
        self._ml_regime = {}
        if self.ml_enabled:
            try:
                from core.ml_filter import MLFilter
                self.ml = MLFilter.load(self.ml_model_file)
                if self.ml is None:
                    self.log.warning("ml_filter 已启用但模型文件缺失(%s), 降级关闭, 请先跑 ml_train.py",
                                     self.ml_model_file)
                    self.ml_enabled = self.ml_gate = self.ml_selector = False
                else:
                    self.log.info("ML 门禁/选择器已加载: gate=%s selector=%s threshold=%.2f",
                                  self.ml_gate, self.ml_selector, self.ml_threshold)
            except Exception as e:
                self.log.warning("ml_filter 加载失败: %s", e)
                self.ml_enabled = self.ml_gate = self.ml_selector = False
        # 自动学习进化器: 加载上次学习到的最优组合, 启动后立即学一轮, 之后每天学
        self.timeframe = cfg["timeframe"]   # 先取配置默认, 学习器组合会覆盖
        lcfg = cfg.get("learner", {})
        self.learner_enabled = bool(lcfg.get("enabled", True)) and "donchian" in self.enabled_modes
        self.learner_days = int(lcfg.get("days", 90))
        self.learner_interval = float(lcfg.get("interval_hours", 24)) * 3600
        if "donchian" in self.enabled_modes:
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
        self.tuning_enabled = bool(tun.get("enabled", False)) and "multi" in self.enabled_modes
        self.tuning_interval = float(tun.get("interval_hours", 6)) * 3600
        self.tuning_cfg = tun
        self._last_tune_ts = time.time()  # 启动后先跑一个周期再调参

    # ---------------- 生命周期 ----------------
    async def start(self):
        ok = await asyncio.to_thread(self.exchange.ping)
        if not ok:
            self.log.warning("测试网连接异常, 继续尝试 (行情接口可能不可用)")
        # 记录主网分级解锁基准 (测试网开跑起点): 仅首次写入, 之后保留
        # (config 中的 mainnet.since 可覆盖此值, 用于回填历史起点计入已运行天数)
        if not self.state.data.get("mainnet_baseline"):
            self.state.data["mainnet_baseline"] = time.time()
            self.state.save()
            self.log.info("主网解锁基准已记录: %s",
                          time.strftime("%Y-%m-%d %H:%M", time.localtime(self.state.data["mainnet_baseline"])))
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

    def update_api(self, key: str = "", secret: str = "",
                   mainnet_key: str = "", mainnet_secret: str = "") -> dict:
        """热更新 API Key/Secret (Web 设置面板): 写 .env + 更新内存

        - key/secret         → 测试网 (BINANCE_TESTNET_API_KEY/SECRET)
        - mainnet_key/secret → 主网 (BINANCE_API_KEY/SECRET)
        只用当前生效网络的凭据去热更新交易所客户端, 避免把测试网 Key 套到主网客户端。
        """
        key, secret = key.strip(), secret.strip()
        mainnet_key, mainnet_secret = mainnet_key.strip(), mainnet_secret.strip()
        from core.config import update_env_api, update_env_mainnet_api

        if key or secret:
            try:
                update_env_api(key, secret)
            except Exception as e:
                return {"ok": False, "msg": f"❌ 写入 .env (测试网) 失败: {e}"}
            self.cfg["api"]["key"] = key
            self.cfg["api"]["secret"] = secret

        if mainnet_key or mainnet_secret:
            try:
                update_env_mainnet_api(mainnet_key, mainnet_secret)
            except Exception as e:
                return {"ok": False, "msg": f"❌ 写入 .env (主网) 失败: {e}"}
            self.cfg["api_mainnet"]["key"] = mainnet_key
            self.cfg["api_mainnet"]["secret"] = mainnet_secret

        # 用当前生效网络的凭据热更新交易所客户端
        ap = self.cfg.get("api_mainnet") if self.cfg.get("network") == "mainnet" else self.cfg.get("api")
        if (ap or {}).get("key") and (ap or {}).get("secret"):
            self.exchange.set_credentials(ap["key"], ap["secret"])

        # 仅当"更新的凭据属于当前生效网络"时才即时验证; 否则切换网络时由 set_network 验证
        # (避免把测试网钱包余额误报成主网, 造成误导)
        active_net = self.cfg.get("network", "testnet")
        updated_for_active = (
            (active_net == "testnet" and (key or secret))
            or (active_net == "mainnet" and (mainnet_key or mainnet_secret))
        )
        if self.cfg["mode"] == "live" and updated_for_active:
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
        if updated_for_active:
            self.state.add_event("info", "🔑 API 已更新 (paper 模式无需验证)")
            return {"ok": True, "msg": "✅ API 已保存"}
        # 跨网络保存 (如测试网模式下填主网 Key): 不即时验证, 切换时自动验证
        net_label = "主网" if (mainnet_key or mainnet_secret) else "测试网"
        self.state.add_event("info", f"🔑 {net_label} API 已保存 (切换到该网络后自动验证)")
        return {"ok": True, "msg": f"✅ {net_label} API 已保存 (切换到该网络后自动验证)"}

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
        network = self.cfg.get("network", "testnet")
        ap = self.cfg.get("api_mainnet") if network == "mainnet" else self.cfg.get("api")
        key = (ap or {}).get("key", "")
        return {
            "mode": self.cfg["mode"],
            "network": network,
            "configured": bool(key),
            "verified": self._api_verified,
            "wallet": self._api_wallet,
            "key_masked": (key[:6] + "****" + key[-4:]) if len(key) > 12 else (key[:4] + "****" if key else ""),
        }

    # ---------------- 主网分级解锁 ----------------
    def mainnet_baseline_ts(self) -> float:
        """测试网开跑基准时间(epoch秒): 优先用 config 的 mainnet.since (可回填历史起点),
        否则用 state 中首次启动自动记录的 mainnet_baseline。"""
        since = (self.cfg.get("mainnet") or {}).get("since") or 0
        if since:
            return float(since)
        return float(self.state.data.get("mainnet_baseline") or 0)

    def mainnet_cap_info(self) -> dict:
        """计算主网分级解锁状态与当前限额。

        档位:
          <30天    locked  cap=0     主网入口未开放
          30~60天  t1      cap=500U
          60~90天  t2      cap=1000U
          >=90天   t3      自定义(默认用 risk.max_total_position_notional)
        返回可用于 Web 展示的 warning 文案与倒计时。
        """
        net = self.cfg.get("network", "testnet")
        baseline = self.mainnet_baseline_ts()
        now = time.time()
        elapsed = (now - baseline) / 86400.0 if baseline else 0.0
        elapsed_days = int(elapsed)
        custom = float((self.cfg.get("mainnet") or {}).get("custom_limit", 0) or 0)

        if elapsed < MAINNET_TIER_DAYS[0]:
            tier, cap, unlocked = "locked", 0.0, False
        elif elapsed < MAINNET_TIER_DAYS[1]:
            tier, cap, unlocked = "t1", MAINNET_TIER_CAPS[0], True
        elif elapsed < MAINNET_TIER_DAYS[2]:
            tier, cap, unlocked = "t2", MAINNET_TIER_CAPS[1], True
        else:
            tier, cap, unlocked = "t3", (
                custom if custom > 0 else float(self.cfg["risk"]["max_total_position_notional"])
            ), True

        # 下一档剩余天数 (向上取整到整天数)
        next_days = None
        for b in MAINNET_TIER_DAYS:
            if elapsed < b:
                next_days = int(math.ceil(b - elapsed))
                break

        if net != "mainnet":
            if not unlocked:
                warning = (f"🧪 主网入口未开放: 测试网还需跑满 {next_days} 天 "
                           f"(满30天解锁, 限额 {MAINNET_TIER_CAPS[0]:.0f}U)")
            else:
                tail = f" (自定义额度未设置, 暂用 {cap:.0f}U)" if tier == "t3" and custom <= 0 else ""
                extra = (" · 跑满60天解锁1000U" if tier == "t1"
                         else " · 跑满90天可自定义额度" if tier == "t2" else "")
                warning = f"💰 主网已开放: 当前档位总持仓限额 {cap:.0f}U{tail}{extra}"
        else:
            tail = f" (自定义额度未设置, 默认 {cap:.0f}U; 建议在设置中指定)" if tier == "t3" and custom <= 0 else ""
            extra = (" · 跑满60天解锁1000U, 跑满90天可自定义" if tier == "t1"
                     else " · 跑满90天可自定义额度" if tier == "t2" else "")
            warning = f"⚠️ 主网真实资金交易中 · 当前总持仓限额 {cap:.0f}U{tail}{extra}"

        return {
            "baseline": baseline,
            "elapsed_days": elapsed_days,
            "tier": tier,
            "cap": round(cap, 2),
            "custom_limit": custom,
            "unlocked": unlocked,
            "next_days": next_days,
            "warning": warning,
        }

    def _apply_mainnet_cap(self, budget: float, exposure: float):
        """主网模式: 把开仓预算夹取到"剩余可开仓额度"内。

        返回 (budget, reason|None): reason 非空表示被主网限额拦截。
        测试网 (network!=mainnet) 原样返回, 不受此限制。
        """
        if self.cfg.get("network") != "mainnet":
            return budget, None
        info = self.mainnet_cap_info()
        cap = info["cap"]
        if cap <= 0:
            return 0.0, "主网未解锁, 禁止开仓"
        remaining = cap - exposure
        if remaining <= 0:
            return 0.0, f"主网总持仓已达限额 {cap:.0f}U (当前敞口 {exposure:.0f}U)"
        return min(budget, remaining), None

    def set_mainnet_custom_limit(self, limit: float) -> dict:
        """设置主网自定义限额 (仅 t3 档位允许; 否则拒绝)。写入 config.yaml 并热生效。"""
        info = self.mainnet_cap_info()
        if info["tier"] != "t3":
            return {"ok": False,
                    "msg": f"🔒 仅跑满90天(t3)后可自定义主网额度, 当前档位 {info['tier']} (限额 {info['cap']:.0f}U)"}
        try:
            limit = float(limit)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "❌ 自定义额度需为数字 (USDT)"}
        if limit <= 0:
            return {"ok": False, "msg": "❌ 自定义额度需为正数 (USDT)"}
        max_total = float(self.cfg["risk"]["max_total_position_notional"])
        if limit > max_total:
            return {"ok": False, "msg": f"❌ 自定义额度不可超过 risk.max_total ({max_total:.0f}U)"}
        try:
            from core.config import update_config_mainnet
            update_config_mainnet({"custom_limit": int(round(limit))})
        except Exception as e:
            return {"ok": False, "msg": f"❌ 写入 config.yaml 失败: {e}"}
        self.cfg.setdefault("mainnet", {})["custom_limit"] = limit
        self.state.add_event("info", f"💰 主网自定义额度已设为 {limit:.0f}U")
        return {"ok": True, "msg": f"✅ 主网自定义额度已设为 {limit:.0f}U", "custom_limit": limit}

    def set_network(self, network: str) -> dict:
        """热切换交易网络 (testnet/mainnet): 重建交易所客户端并落盘, 无需重启

        主网 = 真实资金, 需先配置主网 API Key (BINANCE_API_KEY/SECRET)。switch 前
        前端已二次确认; 这里再做一道服务端校验, 防止误触。
        """
        if network not in ("testnet", "mainnet"):
            return {"ok": False, "msg": "❌ network 仅支持 testnet / mainnet"}
        # 主网分级解锁守卫: 测试网跑满30天前禁止切换 (防止误触真实资金)
        if network == "mainnet":
            info = self.mainnet_cap_info()
            if not info["unlocked"]:
                return {"ok": False, "msg": info["warning"]}
        if network == "mainnet" and not (self.cfg.get("api_mainnet", {}).get("key")
                                         and self.cfg.get("api_mainnet", {}).get("secret")):
            return {"ok": False, "msg": "❌ 主网需先在 ⚙️ 设置 填写主网 API Key/Secret (BINANCE_API_KEY/SECRET)"}
        # 主网属于真实资金: live 模式切换时清空本地持仓, 交由下一轮账户同步重建,
        # 避免基于旧网络持仓误下单
        if self.cfg["mode"] == "live" and network != self.cfg.get("network"):
            self.state.data["positions"] = {}
            self.state.data["orders"] = []
        self.cfg["network"] = network
        # 重建交易所客户端 (base_url + 凭据随 network 改变)
        self.exchange = BinanceFutures(self.cfg)
        self.execution.exchange = self.exchange
        try:
            from core.config import update_config_network
            update_config_network(network)
        except Exception as e:
            return {"ok": False, "msg": f"❌ 写入 config.yaml 失败: {e}"}
        # 重新验证登录 (live 模式)
        self._api_verified = None
        self._api_wallet = None
        if self.cfg["mode"] == "live":
            self._verify_api()
        label = "主网(真实资金)" if network == "mainnet" else "测试网(虚拟资金)"
        self.state.add_event(
            "system" if network == "testnet" else "risk",
            f"🌐 已切换交易网络 → {label}"
            + (f" ⚠️ 真实资金, 当前主网总持仓限额 {info['cap']:.0f}U (档位 {info['tier']})" if network == "mainnet" else ""),
        )
        return {"ok": True, "msg": f"✅ 已切换至 {label}", "network": network}

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
        interval = 300 if "donchian" in self.enabled_modes else self._tf_seconds
        return (time.time() - last) >= interval

    def _log_exit_trigger(self, sym: str, pos: dict, side: str, kind: str,
                          price: float, level: float, trailing: bool) -> None:
        """记录平仓触发原因到日志: 标记价 vs 触发线 + 类型(手动/移动/固定) + 距入场幅度"""
        entry = pos.get("entry") or 0.0
        dist = (level - entry) / entry * 100 if entry else 0.0
        if kind == "tp":
            typ = "手动止盈" if pos.get("manual_tp_active") else "固定止盈"
            cmp = "≥" if side == "LONG" else "≤"
            self.log.info("[%s] 触发%s(TP): 标记价 %.2f %s TP %.2f | 距入场 %+.2f%%",
                          sym, typ, price, cmp, level, dist)
        else:  # sl
            if pos.get("manual_sl_active"):
                typ = "手动止损"
            elif trailing:
                n = pos.get("trail_count", 0)
                d = "上移" if side == "LONG" else "下移"
                typ = f"移动止损(第{n}次{d})"
            else:
                typ = "固定止损"
            cmp = "≤" if side == "LONG" else "≥"
            self.log.info("[%s] 触发%s(SL): 标记价 %.2f %s SL %.2f | 距入场 %+.2f%%",
                          sym, typ, price, cmp, level, dist)

    # ---------------- 止盈止损 ----------------
    async def _check_tp_sl(self):
        d = self.state.data
        sl_atr = float(self.cfg["position"]["sl_atr"])
        for sym, pos in list(d["positions"].items()):
            p = d["mark_prices"].get(sym)
            if not p or p <= 0:
                continue
            # ---- 手动止盈止损: 优先覆盖自动 ATR, 清空后回退自动 ----
            manual_tp = pos.get("manual_tp")
            manual_sl = pos.get("manual_sl")
            if manual_tp or manual_sl:
                entry = pos.get("entry") or 0.0
                sign = 1.0 if pos["side"] == "LONG" else -1.0
                if manual_tp:
                    pos["tp"] = _resolve_manual(entry, sign, manual_tp, True)
                    pos["manual_tp_active"] = True
                if manual_sl:
                    pos["sl"] = _resolve_manual(entry, sign, manual_sl, False)
                    pos["manual_sl_active"] = True
                # 手动止盈+止损一并同步到交易所 (市价单, 行情断流也能成交)
                await self._sync_exchange_orders(sym, pos)
            # 补算 + 移动止损: donchian 与 交易所同步 均走「移动止损」策略
            # - 不挂固定止盈 (让利润奔跑), 只补一个初始 SL, 之后交给 trailing 随新高/新低锁浮盈
            # - 设了手动止盈/止损则优先走用户硬止损, 不覆盖
            # 注意: signals 以 "模式:币种" 为键, 不能裸 sym 取, 否则永远取不到 atr
            sig = next((v for k, v in d["signals"].items() if k.endswith(":" + sym)), {}) or {}
            atr = sig.get("atr") or 0.0
            sign = 1.0 if pos["side"] == "LONG" else -1.0
            trailing = pos.get("mode") in ("donchian", "交易所同步")
            # 1) 缺 SL 且未设手动止损 → 用最近 ATR 补一个初始止损, 避免裸奔
            if not pos.get("sl") and not pos.get("manual_sl") and atr and atr > 0:
                d_sl = (self.donchian.sl_atr if self.donchian else sl_atr) if pos.get("mode") == "donchian" else sl_atr
                pos["sl"] = round(pos["entry"] - sign * d_sl * atr, 2)
                self.log.info("[%s] 补算初始止损 SL=%.2f", sym, pos["sl"])
                self.state.add_event("info", f"🛡 {sym} 补算初始止损 SL={pos['sl']:.2f}")
                # 补算后立即把止损下到交易所
                await self._sync_exchange_orders(sym, pos)
            # 2) 移动止损模式: 不挂固定止盈, 清掉旧的固定止盈值 (让利润奔跑)
            if trailing and not pos.get("manual_tp") and pos.get("tp"):
                pos["tp"] = 0
                self.log.info("[%s] 清除固定止盈, 改用移动止损", sym)

            # ---- 移动止损 (trailing stop): donchian 与 交易所同步 持仓, 随新高/新低锁浮盈 ----
            # 注意: 设了手动止损则跳过移动止损, 否则会被 trailing 改掉用户硬止损
            if trailing and not pos.get("manual_sl"):
                await self._update_trailing_stop(sym, pos, p, atr)

            if pos["side"] == "LONG":
                if pos.get("tp") and p >= pos["tp"]:
                    self._log_exit_trigger(sym, pos, "LONG", "tp", p, pos["tp"], trailing)
                    reason = "手动止盈TP" if pos.get("manual_tp_active") else "固定止盈TP"
                    trade = await self.execution.close_position(sym, p, reason)
                    if trade:
                        await self._notify_close(trade)
                elif pos.get("sl") and p <= pos["sl"]:
                    self._log_exit_trigger(sym, pos, "LONG", "sl", p, pos["sl"], trailing)
                    reason = "手动止损SL" if pos.get("manual_sl_active") else ("移动止损SL" if trailing else "固定止损SL")
                    trade = await self.execution.close_position(sym, p, reason)
                    if trade:
                        await self._notify_close(trade)
            else:
                if pos.get("tp") and p <= pos["tp"]:
                    self._log_exit_trigger(sym, pos, "SHORT", "tp", p, pos["tp"], trailing)
                    reason = "手动止盈TP" if pos.get("manual_tp_active") else "固定止盈TP"
                    trade = await self.execution.close_position(sym, p, reason)
                    if trade:
                        await self._notify_close(trade)
                elif pos.get("sl") and p >= pos["sl"]:
                    self._log_exit_trigger(sym, pos, "SHORT", "sl", p, pos["sl"], trailing)
                    reason = "手动止损SL" if pos.get("manual_sl_active") else ("移动止损SL" if trailing else "固定止损SL")
                    trade = await self.execution.close_position(sym, p, reason)
                    if trade:
                        await self._notify_close(trade)

    async def _update_trailing_stop(self, sym: str, pos: dict, price: float, atr: float) -> None:
        """移动止损: 价格创新高/新低时, 把止损线跟上去, 锁住浮盈

        LONG: 止损 = 持仓期间最高价 - trail_atr×ATR (只上移, 不下移)
        SHORT: 止损 = 持仓期间最低价 + trail_atr×ATR (只下移, 不上移)
        同时同步更新交易所止损单, 保证行情断流也能锁利
        """
        trail_cfg = self.cfg.get("donchian", {}).get("trail", {})
        if not trail_cfg.get("enabled", True):
            return
        trail_atr = float(trail_cfg.get("atr_mult", 2.0))
        min_move = float(trail_cfg.get("min_pct", 0.004))

        # 记录持仓期间的最高/最低价
        if pos["side"] == "LONG":
            pos["high"] = max(pos.get("high") or pos["entry"], price)
            if atr and atr > 0:
                new_sl = pos["high"] - trail_atr * atr
                cur_sl = pos.get("sl") or 0.0
                # 只上移 (锁利), 且至少移动 min_pct 才更新
                if new_sl > cur_sl and (new_sl - cur_sl) / price >= min_move:
                    pos["sl"] = round(new_sl, 2)
                    pos["trail_count"] = pos.get("trail_count", 0) + 1
                    self.log.info("[%s] 移动止损上移: SL %.2f → %.2f (最高 %.2f)",
                                  sym, cur_sl, pos["sl"], pos["high"])
                    self.state.add_event(
                        "info",
                        f"🎯 {sym} 移动止损上移 {cur_sl:.2f} → {pos['sl']:.2f} (最高 {pos['high']:.2f})",
                    )
                    await self._sync_exchange_orders(sym, pos)
        else:  # SHORT
            pos["low"] = min(pos.get("low") or pos["entry"], price)
            if atr and atr > 0:
                new_sl = pos["low"] + trail_atr * atr
                cur_sl = pos.get("sl") or 0.0
                # 只下移 (锁利), 且至少移动 min_pct 才更新
                if 0 < new_sl < cur_sl and (cur_sl - new_sl) / price >= min_move:
                    pos["sl"] = round(new_sl, 2)
                    pos["trail_count"] = pos.get("trail_count", 0) + 1
                    self.log.info("[%s] 移动止损下移: SL %.2f → %.2f (最低 %.2f)",
                                  sym, cur_sl, pos["sl"], pos["low"])
                    self.state.add_event(
                        "info",
                        f"🎯 {sym} 移动止损下移 {cur_sl:.2f} → {pos['sl']:.2f} (最低 {pos['low']:.2f})",
                    )
                    await self._sync_exchange_orders(sym, pos)

    async def _place_with_retry(self, fn, *args, retries: int = 1):
        """下单, 遇 Binance -4130 (closePosition 市价单并发冲突) 重试一次。

        -4130 偶发: 连续挂 TP+SL 两个 closePosition GTE 市价单时, 第一张尚未注册完成,
        第二张会报 "An open stop or take profit order ... is existing"。稍候重试即可成功。
        """
        last = None
        for i in range(retries + 1):
            try:
                return await asyncio.to_thread(fn, *args)
            except Exception as e:  # noqa: BLE001
                last = e
                if "4130" in str(e) and i < retries:
                    await asyncio.sleep(0.4)
                    continue
                raise
        raise last  # type: ignore[misc]

    async def _sync_exchange_orders(self, sym: str, pos: dict, force: bool = False) -> bool:
        """把该持仓在交易所应挂的止盈/止损市价单一次性同步好 (cancel_all + 按本地状态重挂)。

        设计目标: 止盈(TP)与止损(SL)由同一函数管理, 避免各自撤单时互相清掉对方。
        - SL: 只要 pos['sl']>0 就挂 STOP_MARKET (覆盖 手动SL / ATR止损 / 移动止损)
        - TP: 仅 manual_tp 时挂 TAKE_PROFIT_MARKET (自动策略默认让利润奔跑, 不挂TP)
        - 仅在「未挂过 / force / 触发价变化」时才撤单重挂, 避免每 tick 撤挂把 -4130 竞态放大;
          挂单遇 -4130 自动重试一次。
        触发价均经 round_price 取整, 触发即市价成交 (workingType=MARK_PRICE)。
        """
        if self.cfg["mode"] != "live":
            return False
        sl = pos.get("sl") or 0.0
        mtp = pos.get("manual_tp")
        need_sl = sl > 0
        need_tp = bool(mtp)
        if not need_sl and not need_tp:
            # 既无 SL 也无手动 TP: 若之前挂过则清掉
            if pos.get("orders_placed"):
                try:
                    await asyncio.to_thread(self.exchange.cancel_all_orders, sym)
                except Exception:
                    pass
                pos["orders_placed"] = False
            return False
        want_tp = round(float(pos["tp"]), 2) if need_tp else None
        want_sl = round(sl, 2) if need_sl else None
        # 已挂过且价格未变 → 跳过 (省 API 调用, 也避免 -4130 竞态)
        if (not force and pos.get("orders_placed")
                and pos.get("orders_tp") == want_tp and pos.get("orders_sl") == want_sl):
            return True
        side = "SELL" if pos["side"] == "LONG" else "BUY"
        try:
            # 先撤掉旧单 (同一交易对只允许一对止盈止损市价单, 避免重复/冲突)
            await asyncio.to_thread(self.exchange.cancel_all_orders, sym)
            pos["orders_placed"] = False
            if need_sl:
                await self._place_with_retry(self.exchange.place_stop_market, sym, side, sl)
            if need_tp:
                await self._place_with_retry(self.exchange.place_tp_market, sym, side, pos["tp"])
            pos["orders_placed"] = True
            pos["orders_tp"] = want_tp
            pos["orders_sl"] = want_sl
            tp_s = f"TP={want_tp}" if need_tp else "-"
            sl_s = f"SL={want_sl}" if need_sl else "-"
            self.log.info("[%s] 交易所市价止盈止损已挂 %s (%s / %s)", sym, side, tp_s, sl_s)
            self.state.add_event("info", f"🎯 {sym} 交易所市价止盈止损已挂 ({side} @ {tp_s} / {sl_s})")
            return True
        except Exception as e:
            self.log.error("[%s] 同步交易所止盈止损单失败: %s", sym, e)
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
            for sym, pos in list(self.state.data["positions"].items()):
                need_tp = bool(pos.get("manual_tp"))
                need_sl = (pos.get("sl") or 0) > 0
                if not (need_tp or need_sl):
                    continue
                # 已挂过则跳过; 否则补挂 (重启/撤单后自动恢复 市价止盈止损)
                if not pos.get("orders_placed"):
                    await self._sync_exchange_orders(sym, pos)
        except Exception as e:
            self.log.warning("止损单巡检失败: %s", e)

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
        mark = d["mark_prices"].get(symbol) or d["prices"].get(symbol)
        if not mark or mark <= 0:
            return

        # ============ 多模式并行: 每个启用模式独立分析/开仓 ============
        # 同币种竞争制: 该币种已有任何模式的持仓 → 只做该持仓的出场管理, 不再开新仓
        pos = d["positions"].get(symbol)
        for mode in self.enabled_modes:
            sig = self.modes.analyze(mode, symbol, klines, price=mark)
            if sig is None:
                continue
            d["signals"][f"{mode}:{symbol}"] = sig.to_dict()
            # ---- ML 选择器: 震荡市抑制 Donchian 开仓 (出场管理不受影响) ----
            if mode == "donchian" and self.ml_selector and self.ml:
                self._ml_regime[symbol] = self.ml.regime(klines)
                if self._ml_regime[symbol] == "range" and not pos:
                    continue
            # ---- ML 门禁: 低盈利概率的 Donchian 信号不开仓 ----
            if mode == "donchian" and sig.action and self.ml_gate and self.ml and not pos:
                feats = self.ml.features_for_gate(klines, sig, self.donchian)
                prob = self.ml.gate_prob(feats) if feats is not None else 1.0
                if feats is None or prob < self.ml_threshold:
                    self.log.info("🚪 ML门禁拦截 %s %s (prob=%.2f)", symbol, sig.action, prob)
                    self.state.add_event("ml", f"🚪 门禁拦截 {symbol} {sig.action} prob={prob:.2f}")
                    continue
            if not pos:
                # 无持仓: 开仓 (信号触发且通过风控/ML门禁)
                if sig.action == "LONG":
                    await self._try_open_mode(mode, symbol, "LONG", mark, sig)
                elif sig.action == "SHORT":
                    await self._try_open_mode(mode, symbol, "SHORT", mark, sig)
                # 开仓成功则其他模式不再抢同币种
                if d["positions"].get(symbol):
                    break
            else:
                # 有持仓: 出场管理 (donchian 走通道反向; 其他模式走信号消失)
                pos_mode = pos.get("mode", "donchian")
                if mode == pos_mode:
                    if mode == "donchian" and self.donchian:
                        if self.donchian.check_exit(klines, pos):
                            trade = await self.execution.close_position(symbol, mark, "通道反向出场")
                            if trade:
                                await self._notify_close(trade)
                    elif mode != "donchian":
                        close_th = float(self.cfg["signal"].get("close_threshold", 0.0))
                        if close_th > 0:
                            if pos["side"] == "LONG" and sig.score < close_th:
                                trade = await self.execution.close_position(symbol, mark, "信号消失")
                                if trade:
                                    await self._notify_close(trade)
                            elif pos["side"] == "SHORT" and sig.score > -close_th:
                                trade = await self.execution.close_position(symbol, mark, "信号消失")
                                if trade:
                                    await self._notify_close(trade)
                break  # 有持仓时只处理持有该仓的模式

    async def _try_open(self, symbol: str, side: str, price: float, result):
        d = self.state.data
        exposure = self.state.exposure()
        budget = min(
            float(self.cfg["risk"]["max_single_order_notional"]),
            float(self.cfg["risk"]["max_total_position_notional"]) - exposure,
        )
        if budget <= 0:
            return
        # 主网分级限额: 总持仓不得超过当前档位 cap (500/1000/自定义)
        budget, reason = self._apply_mainnet_cap(budget, exposure)
        if budget <= 0:
            if reason:
                self.log.info("[%s] 主网开仓被限额拦截: %s", symbol, reason)
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

    async def _try_open_mode(self, mode: str, symbol: str, side: str, price: float, sig):
        """通用开仓: 支持所有模式

        - donchian: 只设 ATR 止损, 无固定止盈 (移动止损锁利, 通道反向出场)
        - 其他模式: 固定 TP/SL (ATR 倍数)
        - 资金权重: 模式学习器按实盘表现分配的权重, 权重高仓位大
        - 动态杠杆: 强信号高倍, 弱信号低倍
        """
        d = self.state.data
        exposure = self.state.exposure()
        leverage = max(1, int(getattr(sig, "leverage", 1)))
        lev_max = 5
        weight = self.modes.weight_of(mode)
        max_single = float(self.cfg["risk"]["max_single_order_notional"])
        margin_base = float(self.cfg["risk"].get(
            "margin_per_position", max_single / max(1, lev_max)
        ))
        # 名义价值 = 保证金 × 杠杆 × 模式权重
        budget = min(margin_base * leverage * weight, max_single,
                     float(self.cfg["risk"]["max_total_position_notional"]) - exposure)
        if budget <= 0:
            return
        # 主网分级限额: 总持仓不得超过当前档位 cap (500/1000/自定义)
        budget, reason = self._apply_mainnet_cap(budget, exposure)
        if budget <= 0:
            if reason:
                self.log.info("[%s] 主网开仓被限额拦截: %s", symbol, reason)
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

        sign = 1.0 if side == "LONG" else -1.0
        if mode == "donchian":
            # 只设 ATR 止损, 无固定止盈 (移动止损/通道反向出场)
            sl = sig.sl if sig.sl and sig.sl > 0 else price - sign * sig.sl_atr * sig.atr
            tp = None
        else:
            # 固定 TP/SL (ATR 倍数, 带最小距离防手续费吃掉)
            pos_cfg = self.cfg.get("position", {})
            min_tp = price * float(pos_cfg.get("min_tp_pct", 0.0025))
            min_sl = price * float(pos_cfg.get("min_sl_pct", 0.0015))
            tp_atr = sig.tp_atr or float(pos_cfg.get("tp_atr", 2.0))
            sl_atr = sig.sl_atr or float(pos_cfg.get("sl_atr", 1.5))
            tp = price + sign * max(tp_atr * sig.atr, min_tp)
            sl = price - sign * max(sl_atr * sig.atr, min_sl)

        strategy = f"{mode}-{sig.regime}"
        ok = await self.execution.open_position(
            symbol, side, qty, price, leverage, notional, tp, sl, sig.atr, strategy
        )
        if ok:
            mode_label = {"donchian": "Donchian", "multi": "多策略", "grid": "网格",
                          "ma_cross": "均线", "rsi": "RSI", "bollinger": "布林带"}.get(mode, mode)
            # 开仓原因: 触发策略 + 关键信号, 落到引擎日志 (便于复盘为什么开这仓)
            trigger_phrase = {
                "donchian": "唐奇安通道突破(趋势跟踪)",
                "multi": "多策略综合评分转正(自适应)",
                "grid": "价格触及网格边界(均值回归)",
                "ma_cross": "均线金叉/死叉",
                "rsi": "RSI 超卖/超买反转",
                "bollinger": "布林带触轨/开口",
            }.get(mode, mode)
            side_cn = "做多" if side == "LONG" else "做空"
            self.log.info(
                "[%s] 开仓原因[%s]: %s %s | 触发:%s 强度=%.2f 评分=%.2f 市况=%s | "
                "入场=%.2f ATR=%.2f(%.2f%%) 止损=%.2f%s",
                symbol, mode_label, side_cn, side, trigger_phrase,
                sig.strength, sig.score, sig.regime, price,
                sig.atr, sig.atr_pct * 100, sl,
                (f" 止盈={tp:.2f}" if tp else " (移动止损/让利润奔跑)"),
            )
            self.state.add_event(
                "trade",
                f"🟢 [{mode_label}] 开仓 {symbol} {'做多' if side == 'LONG' else '做空'} "
                f"{qty} @ {price:.2f} | 杠杆 {leverage}x | 强度 {sig.strength:.2f} | "
                f"权重 {weight:.1f}x | 止损 {sl:.2f}" + (f" | 止盈 {tp:.2f}" if tp else " | 移动止损"),
            )
            await self._notify(
                f"🟢 [{mode_label}] 开仓 {symbol}\n"
                f"方向: {'做多' if side == 'LONG' else '做空'}\n"
                f"数量: {qty} | 价格: {price:.2f}\n"
                f"杠杆: {leverage}x | 信号强度: {sig.strength:.2f} | 权重: {weight:.1f}x\n"
                f"止损: {sl:.2f}" + (f"\n止盈: {tp:.2f}" if tp else "\n出场: 移动止损/通道反向")
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

        # ---- 多模式: 按实盘表现更新资金权重 (赚钱的多给资金) ----
        if len(self.enabled_modes) > 1:
            try:
                mode_stats = self.state.data.get("mode_stats", {})
                weights = learn_mode_weights(mode_stats, enabled=self.enabled_modes)
                if weights:
                    old_w = dict(self.modes.weights)
                    self.modes.update_weights(weights)
                    self.state.add_event(
                        "tuning",
                        "🎯 模式资金权重更新: " + " | ".join(
                            f"{m}={self.modes.weights.get(m, 1.0):.2f}x" for m in self.enabled_modes
                        ),
                    )
                    self.log.info("模式资金权重已更新: %s", self.modes.weights)
            except Exception as e:
                self.log.error("模式权重学习失败: %s", e)
