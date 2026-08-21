#!/usr/bin/env python3
"""Binance 测试网自动量化交易系统 入口

用法:
    python run.py            # 读取 config.yaml 启动 (paper 或 live 由配置决定)
"""
from __future__ import annotations

import asyncio

import uvicorn

from core.config import load_config
from core.logger import setup_logging
from engine.accounts import AccountManager
from web.app import create_app


async def main():
    cfg = load_config()
    log = setup_logging(cfg)
    log.info("=" * 60)
    log.info("Binance 测试网自动量化交易系统启动")
    log.info("模式: %s | 周期: %s | 交易对: %s", cfg["mode"], cfg["timeframe"], cfg["symbols"])

    manager = AccountManager(cfg)
    await manager.start()

    web_cfg = cfg.get("web", {"host": "127.0.0.1", "port": 8090})
    app = create_app(manager)
    config = uvicorn.Config(
        app, host=web_cfg["host"], port=int(web_cfg["port"]), log_level="warning"
    )
    server = uvicorn.Server(config)
    names = ", ".join(a.spec.name for a in manager.accounts)
    log.info("Web 仪表盘: http://%s:%s | 账户: %s", web_cfg["host"], web_cfg["port"], names)
    try:
        await server.serve()
    finally:
        await manager.stop()
        log.info("系统已停止")


if __name__ == "__main__":
    asyncio.run(main())
