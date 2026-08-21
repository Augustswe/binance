"""FastAPI 应用: 仪表盘页面 + 状态API + 控制API (多账户账户感知)

所有「读/写当前账户」的端点按 ?account=<name> 解析到对应子引擎;
无 account 参数 → 默认(列表第一个)账户。新增账户聚合与绑定/解绑/启停/重置端点。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.autostart import (disable as autostart_disable,
                            enable as autostart_enable,
                            status as autostart_status)
from engine.accounts import AccountManager, AccountSpec, STRATEGY_LABELS
from engine.trader import _resolve_manual
from engine.accounts import VALID_MODES

STATIC_DIR = Path(__file__).resolve().parent / "static"


class ControlBody(BaseModel):
    action: str


class ApiBody(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    mainnet_key: str = ""
    mainnet_secret: str = ""


class SymbolsBody(BaseModel):
    symbols: list[str]


class ModesBody(BaseModel):
    modes: list[str]


class RiskBody(BaseModel):
    risk: dict = {}
    leverage: dict = {}


class NetworkBody(BaseModel):
    network: str = "testnet"


class MainnetQuotaBody(BaseModel):
    custom_limit: float = 0.0


class AutostartBody(BaseModel):
    enabled: bool = False


class InitialCapitalBody(BaseModel):
    value: float = 0.0


class RunModeBody(BaseModel):
    mode: str = "auto"


class ManualTPSLBody(BaseModel):
    symbol: str
    tp: dict | None = None   # {"type":"price"|"pct","value":<number>} 或 None=清除
    sl: dict | None = None


class ClosePositionBody(BaseModel):
    symbol: str


class BindBody(BaseModel):
    name: str
    network: str = "testnet"
    mode: str = "live"
    api_key: str = ""
    api_secret: str = ""
    run_mode: str = "auto"
    symbols: list[str] | None = None
    modes_enabled: list[str] | None = None


class NameBody(BaseModel):
    name: str
    enabled: bool = True


def create_app(manager: AccountManager) -> FastAPI:
    app = FastAPI(title="Binance 测试网量化仪表盘", docs_url=None, redoc_url=None)

    def _engine(request: Request):
        return manager.get_engine(request.query_params.get("account"))

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        """页面与静态资源禁用缓存, 保证仪表盘永远加载最新代码和数据"""
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    async def api_state(request: Request):
        e = _engine(request)
        snap = e.get_snapshot()
        snap["api"] = e.api_status()        # 登录状态徽章用
        snap["network"] = e.cfg["network"]  # 主网红色横幅用
        snap["mainnet"] = e.mainnet_cap_info()  # 主网分级解锁/限额信息
        snap["account"] = e.name
        snap["strategy_label"] = STRATEGY_LABELS.get(e.run_mode, e.run_mode)
        return snap

    @app.post("/api/logs/clear")
    async def api_logs_clear(request: Request):
        """清空仪表盘操作日志 (前端终端「清屏」按钮调用). 仅清内存中的事件, 不影响持仓/权益."""
        n = _engine(request).state.clear_events()
        return {"ok": True, "cleared": n, "msg": f"✅ 已清空 {n} 条日志"}

    @app.get("/api/config")
    async def api_config(request: Request):
        cfg = _engine(request).cfg
        return {
            "mode": cfg["mode"],
            "network": cfg["network"],
            "timeframe": cfg["timeframe"],
            "symbols": cfg["symbols"],
            "leverage": cfg["leverage"],
            "risk": cfg["risk"],
            "strategies": cfg["strategies"],
            "tuned_meta": cfg.get("tuned_meta", {}),
            "position": cfg["position"],
            "fees": cfg["fees"],
            "signal": cfg["signal"],
        }

    @app.get("/api/learner")
    async def api_learner():
        """自动学习器状态: 当前组合 + 历史轮次排名 (供学习历史面板)"""
        from core.learner import load_learner_state
        state = load_learner_state()
        rounds = state.get("rounds", [])
        return {
            "current": state.get("current"),
            "best_score": state.get("best_score"),
            "learned_at": state.get("learned_at"),
            "rounds": [
                {
                    "ts": r.get("ts"),
                    "best_key": r.get("best_key"),
                    "best_score": r.get("best_score"),
                    "top3": [
                        {"key": x["key"], "score": x["score"]}
                        for x in r.get("rankings", [])[:3]
                    ],
                }
                for r in rounds[-15:]
            ],
        }

    @app.get("/api/ml_filter")
    async def api_ml_filter(request: Request):
        """ML 门禁/选择器状态: 是否启用、模型是否已加载、各币当前 regime、walk-forward 指标"""
        e = _engine(request)
        return {
            "enabled": e.ml_enabled,
            "gate": e.ml_gate,
            "selector": e.ml_selector,
            "threshold": e.ml_threshold,
            "model_loaded": e.ml is not None,
            "model_file": e.ml_model_file,
            "regime": getattr(e, "_ml_regime", {}),
            "metrics": (e.ml.metrics if e.ml else None),
        }

    @app.post("/api/ml_filter/train")
    async def api_ml_filter_train(request: Request):
        """离线训练 ML 门禁/选择器模型 (拉主网历史K线, walk-forward 评估, 保存)"""
        import asyncio
        from core.ml_filter import MLFilter
        e = _engine(request)
        try:
            model = await asyncio.to_thread(
                MLFilter.train, e.cfg,
                int(e.cfg.get("ml_filter", {}).get("days", 365)),
            )
            model.save(e.ml_model_file)
            e.ml = model
            e.ml_enabled = True
            e.ml_gate = bool(e.cfg.get("ml_filter", {}).get("gate", True))
            e.ml_selector = bool(e.cfg.get("ml_filter", {}).get("selector", True))
            return {"ok": True, "threshold": model.threshold, "metrics": model.metrics}
        except Exception as ex:
            return {"ok": False, "error": str(ex)[:200]}

    @app.post("/api/control")
    async def api_control(body: ControlBody, request: Request):
        return _engine(request).control(body.action)

    # ---------------- 手动止盈止损 ----------------
    @app.post("/api/manual_tp_sl")
    async def api_manual_tp_sl(body: ManualTPSLBody, request: Request):
        """手动设置/清除某持仓的止盈(TP)/止损(SL)。仅作用于当前已开仓。

        - tp/sl 为 {type,value} 时覆盖自动 ATR; 为 null 时清除该侧, 回退到自动。
        - 立即解析为绝对价格写入 pos, 并(在 live 模式)把 止盈+止损 市价单一并挂到
          交易所 (TAKE_PROFIT_MARKET / STOP_MARKET, 触发即市价成交, 即使 bot 掉线也生效)。
        """
        e = _engine(request)
        d = e.state.data
        pos = d["positions"].get(body.symbol)
        if not pos:
            return {"ok": False, "error": f"当前无 {body.symbol} 持仓"}
        for name, spec in (("tp", body.tp), ("sl", body.sl)):
            if spec is not None:
                if spec.get("type") not in ("price", "pct") or not isinstance(spec.get("value"), (int, float)):
                    return {"ok": False, "error": f"{name} 格式错误: 需 {{type:price|pct, value:数字}}"}
        sign = 1.0 if pos["side"] == "LONG" else -1.0
        entry = pos.get("entry") or 0.0
        if body.tp is None:
            pos.pop("manual_tp", None)
            pos.pop("manual_tp_active", None)
            pos["tp"] = None
        else:
            pos["manual_tp"] = body.tp
            pos["tp"] = _resolve_manual(entry, sign, body.tp, True)
        if body.sl is None:
            pos.pop("manual_sl", None)
            pos.pop("manual_sl_active", None)
            pos["sl"] = None
        else:
            pos["manual_sl"] = body.sl
            pos["sl"] = _resolve_manual(entry, sign, body.sl, False)
        if e.cfg["mode"] == "live":
            try:
                await e._sync_exchange_orders(body.symbol, pos, force=True)
            except Exception as ex:
                e.log.warning("手动止盈止损下单失败: %s", ex)
        e.state.save()
        e.state.add_event(
            "info",
            f"🛡 {body.symbol} 手动止盈止损已更新: TP={pos.get('tp')} SL={pos.get('sl')}",
        )
        return {"ok": True, "symbol": body.symbol,
                "tp": pos.get("tp"), "sl": pos.get("sl"),
                "manual_tp": pos.get("manual_tp"), "manual_sl": pos.get("manual_sl")}

    # ---------------- 手动市价平仓 ----------------
    @app.post("/api/close_position")
    async def api_close_position(body: ClosePositionBody, request: Request):
        """手动按当前价立即市价平仓单个持仓 (不可撤销)。

        - 取当前 mark 价作为参考价; live 模式先撤掉交易所侧挂单(止盈/止损),
          再下 reduce-only 市价单真实成交; paper 模式直接按参考价结算。
        - 与 trader 主循环里的 _check_tp_sl 走同一条 execution.close_position 路径。
        """
        e = _engine(request)
        d = e.state.data
        sym = body.symbol
        pos = d["positions"].get(sym)
        if not pos:
            return {"ok": False, "error": f"当前无 {sym} 持仓"}
        mark = d["prices"].get(sym, 0.0)
        try:
            trade = await e.execution.close_position(sym, mark, "手动市价平仓")
        except Exception as ex:
            return {"ok": False, "error": f"平仓失败: {str(ex)[:200]}"}
        if not trade:
            return {"ok": False, "error": f"{sym} 平仓未成交 (可能已无持仓)"}
        pnl = trade.get("pnl")
        pnl_pct = trade.get("pnl_pct")
        e.state.add_event(
            "info",
            f"🔒 {sym} 手动市价平仓完成: 盈亏 {pnl:.2f}U ({pnl_pct:.2f}%)" if isinstance(pnl, (int, float)) else f"🔒 {sym} 手动市价平仓完成",
        )
        return {"ok": True, "symbol": sym, "trade": trade}

    # ---------------- 设置面板 ----------------
    @app.get("/api/settings")
    async def api_settings(request: Request):
        """返回设置面板需要的数据: 当前币种 + 模式 + API 登录状态 + 可用币种候选 + 账户信息"""
        e = _engine(request)
        cfg = e.cfg
        try:
            candidates = e.exchange.search_symbols("", limit=200)
        except Exception:
            candidates = []
        from strategies.modes import ALL_MODES

        return {
            "mode": cfg["mode"],
            "symbols": list(cfg["symbols"]),
            "all_modes": ALL_MODES,
            "enabled_modes": list(e.enabled_modes),
            "mode_weights": dict(e.modes.weights),
            "api": e.api_status(),
            "candidates": [c["symbol"] for c in candidates],
            "has_positions": list(e.state.data["positions"].keys()),
            "risk": dict(e.cfg["risk"]),
            "leverage": dict(e.cfg["leverage"]),
            "network": e.cfg.get("network", "testnet"),
            "mainnet_configured": bool(
                (e.cfg.get("api_mainnet") or {}).get("key")
                and (e.cfg.get("api_mainnet") or {}).get("secret")
            ),
            "mainnet": e.mainnet_cap_info(),
            "autostart": autostart_status(),
            "initial_capital": float(e.state.data.get("initial_capital", 0.0)),
            "equity": e.state.equity(),
            "run_mode": e.run_mode,
            # 多账户信息: 账户名 / 策略中文 / 是否锁定(非 default 不可热改策略)
            "account": e.name,
            "strategy_label": STRATEGY_LABELS.get(e.run_mode, e.run_mode),
            "strategy_locked": e.name != "default",
        }

    @app.post("/api/settings/initial_capital")
    async def api_settings_initial_capital(body: InitialCapitalBody, request: Request):
        """设置起始资金 (盈亏模块): 持久化到 state.json, 即时生效"""
        e = _engine(request)
        try:
            e.state.set_initial_capital(body.value)
        except ValueError as ex:
            return {"ok": False, "msg": str(ex)}
        return {
            "ok": True,
            "initial_capital": e.state.data.get("initial_capital", 0.0),
            "equity": e.state.equity(),
            "msg": f"✅ 起始资金已设为 {body.value:.2f} U",
        }

    @app.post("/api/settings/run_mode")
    async def api_settings_run_mode(body: RunModeBody, request: Request):
        """设置运行模式: auto = 全部并行; 或指定某一策略独占开仓权。
        信号面板始终展示所有启用模式的判断, 仅开仓权受此值约束。写 config + 热更新引擎。
        (多账户场景下前端对 strategy_locked 账户隐藏此切换器)"""
        e = _engine(request)
        if e.name != "default" and body.mode != e.run_mode:
            return {"ok": False, "msg": "❌ 该账户策略已绑定, 不可在面板热改(请到入口页重置账户后重绑)"}
        if body.mode not in VALID_MODES:
            return {"ok": False, "msg": f"❌ run_mode 非法: {body.mode}"}
        e.run_mode = body.mode
        e.state.add_event("info", f"⚙️ 运行模式已切换: {body.mode}")
        return {"ok": True, "run_mode": e.run_mode, "msg": f"✅ 运行模式已设为 {body.mode}"}

    @app.post("/api/settings/modes")
    async def api_settings_modes(body: ModesBody, request: Request):
        """切换启用的策略模式 (写 config + 热更新引擎)"""
        from core.config import update_config_modes
        from strategies.modes import ALL_MODES

        e = _engine(request)
        modes = [m for m in body.modes if m in ALL_MODES]
        if not modes:
            return {"ok": False, "msg": "❌ 至少需要启用一个模式"}
        try:
            update_config_modes(modes)
        except Exception as ex:
            return {"ok": False, "msg": f"❌ 写入 config.yaml 失败: {ex}"}
        from strategies.modes import ModeManager
        e.modes = ModeManager(e.cfg, enabled_modes=modes)
        e.enabled_modes = e.modes.enabled
        e.donchian = e.modes.donchian
        from core.learner import load_mode_weights
        learned_w = load_mode_weights()
        if learned_w:
            e.modes.update_weights(learned_w)
        e.state.add_event("info", f"⚙️ 策略模式已切换: {', '.join(modes)}")
        return {"ok": True, "msg": f"✅ 已启用模式: {', '.join(modes)}", "modes": modes}

    @app.get("/api/symbols/search")
    async def api_symbols_search(q: str = "", limit: int = 50, request: Request = None):
        """按名称模糊搜索可交易币种 (如 q=btc → BTCUSDT)"""
        try:
            res = _engine(request).exchange.search_symbols(q, limit=int(limit))
        except Exception as ex:
            return {"error": str(ex), "results": []}
        return {"results": res}

    @app.post("/api/settings/api")
    async def api_settings_api(body: ApiBody, request: Request):
        e = _engine(request)
        spec = manager.get_spec(e.name)
        key_env = secret_env = None
        if e.name != "default" and spec:
            key_env, secret_env = spec.api_key_env, spec.api_secret_env
        return e.update_api(body.api_key, body.api_secret,
                            body.mainnet_key, body.mainnet_secret,
                            key_env=key_env, secret_env=secret_env)

    @app.post("/api/settings/network")
    async def api_settings_network(body: NetworkBody, request: Request):
        """切换交易网络 (testnet/mainnet): 热生效, 无需重启"""
        return _engine(request).set_network(body.network)

    @app.post("/api/settings/mainnet-quota")
    async def api_settings_mainnet_quota(body: MainnetQuotaBody, request: Request):
        """设置主网自定义限额 (仅跑满90天/t3 档位允许); 否则拒绝。"""
        return _engine(request).set_mainnet_custom_limit(body.custom_limit)

    @app.post("/api/settings/autostart")
    async def api_settings_autostart(body: AutostartBody):
        """开关开机自启 (macOS launchd): enabled=true 安装并加载 LaunchAgent,
        enabled=false 卸载。当前手动运行的实例不受影响, 下次登录/开机由 launchd 接管。"""
        return autostart_enable() if body.enabled else autostart_disable()

    @app.post("/api/settings/symbols")
    async def api_settings_symbols(body: SymbolsBody, request: Request):
        return _engine(request).update_symbols(body.symbols)

    @app.post("/api/settings/risk")
    async def api_settings_risk(body: RiskBody, request: Request):
        """保存风控与敞口参数 (写 config + 热更新引擎, 无需重启)"""
        e = _engine(request)
        def to_int(v):
            try:
                return int(round(float(v)))
            except (TypeError, ValueError):
                return None

        def to_float(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        risk_out: dict = {}
        raw_risk = body.risk or {}
        int_keys = ("max_single_order_notional", "max_total_position_notional",
                    "margin_per_position", "max_positions", "cooldown_minutes")
        for k in int_keys:
            if k in raw_risk:
                v = to_int(raw_risk[k])
                if v is None or v < 0:
                    return {"ok": False, "msg": f"❌ 风控项 {k} 需为非负整数"}
                risk_out[k] = v
        if "daily_loss_stop" in raw_risk:
            v = to_float(raw_risk["daily_loss_stop"])
            if v is None or v <= 0 or v > 1:
                return {"ok": False, "msg": "❌ 日亏熔断需为 0~1 的小数 (如 0.30)"}
            risk_out["daily_loss_stop"] = v

        lev_out: dict = {}
        raw_lev = body.leverage or {}
        if "mode" in raw_lev and raw_lev["mode"] in ("auto", "fixed"):
            lev_out["mode"] = raw_lev["mode"]
        for k in ("min", "max", "fixed"):
            if k in raw_lev:
                v = to_int(raw_lev[k])
                if v is None or v < 1 or v > 20:
                    return {"ok": False, "msg": f"❌ 杠杆 {k} 需为 1~20 的整数"}
                lev_out[k] = v
        if "min" in lev_out and "max" in lev_out and lev_out["min"] > lev_out["max"]:
            return {"ok": False, "msg": "❌ 杠杆 min 不能大于 max"}

        try:
            from core.config import update_config_risk
            update_config_risk(risk_out, lev_out)
        except Exception as ex:
            return {"ok": False, "msg": f"❌ 写入 config.yaml 失败: {ex}"}

        for k, v in risk_out.items():
            e.cfg["risk"][k] = v
        for k, v in lev_out.items():
            e.cfg["leverage"][k] = v

        e.state.add_event("info", "🛡 风控/敞口参数已更新并热生效")
        return {
            "ok": True,
            "msg": "✅ 风控与敞口参数已保存并热更新",
            "risk": dict(e.cfg["risk"]),
            "leverage": dict(e.cfg["leverage"]),
        }

    # ---------------- 多账户: 列表 / 总览 / 绑定 / 解绑 / 启停 / 重置 ----------------
    @app.get("/api/accounts")
    async def api_accounts():
        """账户列表 + 策略中文名 + 是否多账户 + 默认账户名"""
        default_name = manager.get_spec(None).name
        multi = any(a.spec.name != "default" for a in manager.accounts)
        return {
            "accounts": manager.list_accounts(),
            "strategies": STRATEGY_LABELS,
            "default": default_name,
            "multi": multi,
        }

    @app.get("/api/accounts/overview")
    async def api_accounts_overview():
        """多账户盈亏对比聚合 (入口页总览用)"""
        return manager.overview()

    @app.post("/api/accounts/bind")
    async def api_accounts_bind(body: BindBody):
        """绑定新 API: 写 .env(按账户变量名) + accounts.yaml + 热加载新引擎, 无需重启"""
        from core.config import update_env_named
        name = (body.name or "").strip()
        if not name:
            return {"ok": False, "msg": "❌ 账户名不能为空"}
        if body.run_mode not in VALID_MODES:
            return {"ok": False, "msg": f"❌ 策略(run_mode)非法: {body.run_mode}"}
        if body.network not in ("testnet", "mainnet"):
            return {"ok": False, "msg": "❌ network 仅支持 testnet / mainnet"}
        if body.mode not in ("paper", "live"):
            return {"ok": False, "msg": "❌ mode 仅支持 paper / live"}
        if body.mode == "live" and (not body.api_key or not body.api_secret):
            return {"ok": False, "msg": "❌ live 模式必须填写 API Key / Secret"}
        prefix = "BINANCE_API_KEY_" if body.network == "mainnet" else "BINANCE_TESTNET_API_KEY_"
        sprefix = "BINANCE_API_SECRET_" if body.network == "mainnet" else "BINANCE_TESTNET_API_SECRET_"
        key_env = f"{prefix}{name.upper()}"
        secret_env = f"{sprefix}{name.upper()}"
        try:
            update_env_named(key_env, secret_env, body.api_key, body.api_secret)
        except Exception as ex:
            return {"ok": False, "msg": f"❌ 写入 .env 失败: {ex}"}
        os.environ[key_env] = body.api_key.strip()
        os.environ[secret_env] = body.api_secret.strip()
        spec = AccountSpec(
            name=name, enabled=True, network=body.network, mode=body.mode,
            api_key_env=key_env, api_secret_env=secret_env, run_mode=body.run_mode,
            symbols=body.symbols or None, modes_enabled=body.modes_enabled or None,
        )
        try:
            await manager.bind(spec)
        except ValueError as ex:
            return {"ok": False, "msg": str(ex)}
        return {"ok": True, "name": name, "strategy": body.run_mode,
                "msg": f"✅ 已绑定账户 {name} (策略 {STRATEGY_LABELS.get(body.run_mode, body.run_mode)})"}

    @app.post("/api/accounts/unbind")
    async def api_accounts_unbind(body: NameBody):
        """解绑账户: 停引擎 + 从内存与 yaml 移除 (state 文件保留备查)"""
        if body.name == "default":
            return {"ok": False, "msg": "❌ 默认账户不可解绑"}
        await manager.unbind(body.name)
        return {"ok": True, "msg": f"✅ 已解绑账户 {body.name}"}

    @app.post("/api/accounts/toggle")
    async def api_accounts_toggle(body: NameBody):
        """启停账户 (enabled=false 停引擎, true 重建并启动)"""
        await manager.set_enabled(body.name, body.enabled)
        return {"ok": True, "name": body.name, "enabled": body.enabled,
                "msg": f"✅ 账户 {body.name} {'已启用' if body.enabled else '已停用'}"}

    @app.post("/api/accounts/reset")
    async def api_accounts_reset(body: NameBody):
        """重置账户: 清空该账户 state.json(持仓/成交/统计), 引擎重启从交易所重新同步。
        友好提示由前端确认框完成, 这里只执行重置。"""
        await manager.reset_account(body.name)
        return {"ok": True, "name": body.name, "msg": f"✅ 账户 {body.name} 已重置 (历史已清空)"}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
