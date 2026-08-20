"""FastAPI 应用: 仪表盘页面 + 状态API + 控制API"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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


def create_app(engine) -> FastAPI:
    app = FastAPI(title="Binance 测试网量化仪表盘", docs_url=None, redoc_url=None)

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
    async def api_state():
        snap = engine.get_snapshot()
        snap["api"] = engine.api_status()   # 登录状态徽章用
        snap["network"] = engine.cfg["network"]   # 主网红色横幅用
        snap["mainnet"] = engine.mainnet_cap_info()   # 主网分级解锁/限额信息 (横幅与档位展示)
        return snap

    @app.get("/api/config")
    async def api_config():
        cfg = engine.cfg
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
        # 只返回最近15轮的排名摘要
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

    @app.post("/api/control")
    async def api_control(body: ControlBody):
        return engine.control(body.action)

    # ---------------- 设置面板 ----------------
    @app.get("/api/settings")
    async def api_settings():
        """返回设置面板需要的数据: 当前币种 + 模式 + API 登录状态 + 可用币种候选"""
        cfg = engine.cfg
        try:
            candidates = engine.exchange.search_symbols("", limit=200)
        except Exception as e:
            candidates = []
        from strategies.modes import ALL_MODES

        return {
            "mode": cfg["mode"],
            "symbols": list(cfg["symbols"]),
            "all_modes": ALL_MODES,
            "enabled_modes": list(engine.enabled_modes),
            "mode_weights": dict(engine.modes.weights),
            "api": engine.api_status(),
            "candidates": [c["symbol"] for c in candidates],
            "has_positions": list(engine.state.data["positions"].keys()),
            "risk": dict(engine.cfg["risk"]),
            "leverage": dict(engine.cfg["leverage"]),
            "network": engine.cfg.get("network", "testnet"),
            "mainnet_configured": bool(
                (engine.cfg.get("api_mainnet") or {}).get("key")
                and (engine.cfg.get("api_mainnet") or {}).get("secret")
            ),
            "mainnet": engine.mainnet_cap_info(),   # 解锁倒计时/档位/限额/警告
        }

    @app.post("/api/settings/modes")
    async def api_settings_modes(body: ModesBody):
        """切换启用的策略模式 (写 config.yaml + 热更新引擎)"""
        from core.config import update_config_modes
        from strategies.modes import ALL_MODES

        modes = [m for m in body.modes if m in ALL_MODES]
        if not modes:
            return {"ok": False, "msg": "❌ 至少需要启用一个模式"}
        try:
            update_config_modes(modes)
        except Exception as e:
            return {"ok": False, "msg": f"❌ 写入 config.yaml 失败: {e}"}
        # 热更新: 重建模式管理器
        from strategies.modes import ModeManager
        engine.modes = ModeManager(engine.cfg, enabled_modes=modes)
        engine.enabled_modes = engine.modes.enabled  # 以 ModeManager 为准
        engine.donchian = engine.modes.donchian
        from core.learner import load_mode_weights
        learned_w = load_mode_weights()
        if learned_w:
            engine.modes.update_weights(learned_w)
        engine.state.add_event("info", f"⚙️ 策略模式已切换: {', '.join(modes)}")
        return {"ok": True, "msg": f"✅ 已启用模式: {', '.join(modes)}", "modes": modes}

    @app.get("/api/symbols/search")
    async def api_symbols_search(q: str = "", limit: int = 50):
        """按名称模糊搜索可交易币种 (如 q=btc → BTCUSDT)"""
        try:
            res = engine.exchange.search_symbols(q, limit=int(limit))
        except Exception as e:
            return {"error": str(e), "results": []}
        return {"results": res}

    @app.post("/api/settings/api")
    async def api_settings_api(body: ApiBody):
        return engine.update_api(body.api_key, body.api_secret,
                                  body.mainnet_key, body.mainnet_secret)

    @app.post("/api/settings/network")
    async def api_settings_network(body: NetworkBody):
        """切换交易网络 (testnet/mainnet): 热生效, 无需重启

        主网 = 真实资金, engine.set_network 内含服务端校验 (未解锁禁止切换 / 主网必须有 Key) 与
        live 模式下的持仓清空, 防止误触。
        """
        return engine.set_network(body.network)

    @app.post("/api/settings/mainnet-quota")
    async def api_settings_mainnet_quota(body: MainnetQuotaBody):
        """设置主网自定义限额 (仅跑满90天/t3 档位允许); 否则拒绝。

        写入 config.yaml 并热生效, 重启后保留。
        """
        return engine.set_mainnet_custom_limit(body.custom_limit)

    @app.post("/api/settings/symbols")
    async def api_settings_symbols(body: SymbolsBody):
        return engine.update_symbols(body.symbols)

    @app.post("/api/settings/risk")
    async def api_settings_risk(body: RiskBody):
        """保存风控与敞口参数 (写 config.yaml + 热更新引擎, 无需重启)

        风控读取路径: _try_open_mode 读 engine.cfg["risk"], check_open 读
        RiskManager.risk (与 cfg["risk"] 同一字典引用)。原地修改该字典即可让运行中
        的开仓预算与风控校验立即生效。
        """
        # 类型矫正: 数值字段按配置语义转换
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
        except Exception as e:
            return {"ok": False, "msg": f"❌ 写入 config.yaml 失败: {e}"}

        # 热更新: 原地修改 cfg 字典 (RiskManager 持有同一引用)
        for k, v in risk_out.items():
            engine.cfg["risk"][k] = v
        for k, v in lev_out.items():
            engine.cfg["leverage"][k] = v

        engine.state.add_event("info", "🛡 风控/敞口参数已更新并热生效")
        return {
            "ok": True,
            "msg": "✅ 风控与敞口参数已保存并热更新",
            "risk": dict(engine.cfg["risk"]),
            "leverage": dict(engine.cfg["leverage"]),
        }

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
