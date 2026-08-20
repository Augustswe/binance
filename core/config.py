"""配置加载: config.yaml + .env 覆盖 + tuned_params.json 自动合并"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _merge_tuned(cfg: dict) -> None:
    """合并 AI 调参结果 (data/tuned_params.json) 到配置"""
    from .tuner import load_tuned_params

    tuned = load_tuned_params()
    if not tuned:
        return
    for section in ("signal", "position", "strategies"):
        if section in tuned and isinstance(tuned[section], dict):
            cfg.setdefault(section, {})
            if section == "strategies":
                for strat, params in tuned[section].items():
                    if isinstance(params, dict):
                        cfg[section].setdefault(strat, {}).update(params)
            else:
                cfg[section].update(tuned[section])
    cfg["tuned_meta"] = tuned.get("meta", {})


def load_config(path: str | None = None) -> dict:
    """加载 config.yaml, 用 .env 覆盖, 并合并 AI 调参结果"""
    load_dotenv(BASE_DIR / ".env")
    cfg_path = Path(path) if path else BASE_DIR / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # .env 覆盖
    env_mode = os.getenv("TRADING_MODE", "").strip().lower()
    if env_mode in ("paper", "live"):
        cfg["mode"] = env_mode
    cfg["api"] = {
        "key": os.getenv("BINANCE_TESTNET_API_KEY", "").strip(),
        "secret": os.getenv("BINANCE_TESTNET_API_SECRET", "").strip(),
    }
    # Telegram
    cfg.setdefault("telegram", {})
    cfg["telegram"]["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    cfg["telegram"]["chat_id"] = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    # 合并 AI 调参结果
    _merge_tuned(cfg)
    return cfg
