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


class SymbolsBody(BaseModel):
    symbols: list[str]


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
        return snap

    @app.get("/api/config")
    async def api_config():
        cfg = engine.cfg
        return {
            "mode": cfg["mode"],
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
        """返回设置面板需要的数据: 当前币种 + API 登录状态 + 可用币种候选"""
        cfg = engine.cfg
        try:
            candidates = engine.exchange.search_symbols("", limit=200)
        except Exception as e:
            candidates = []
        return {
            "mode": cfg["mode"],
            "symbols": list(cfg["symbols"]),
            "api": engine.api_status(),
            "candidates": [c["symbol"] for c in candidates],
            "has_positions": list(engine.state.data["positions"].keys()),
        }

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
        return engine.update_api(body.api_key, body.api_secret)

    @app.post("/api/settings/symbols")
    async def api_settings_symbols(body: SymbolsBody):
        return engine.update_symbols(body.symbols)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
