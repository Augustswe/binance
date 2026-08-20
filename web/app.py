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
        return engine.get_snapshot()

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

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
