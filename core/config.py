"""配置加载: config.yaml + .env 覆盖 + tuned_params.json 自动合并"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def update_config_symbols(symbols: list[str]) -> None:
    """更新 config.yaml 的 symbols 列表 (文本级替换, 保留注释)

    Web 设置面板添加/删除币种时调用, 重启后依然生效。
    """
    path = BASE_DIR / "config.yaml"
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")

    # 找到 "symbols:" 顶层键, 替换其后到下一个顶层键之间的列表项
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^symbols:\s*$", line)
        if m and not replaced:
            out.append(line)
            i += 1
            # 跳过旧的列表项
            while i < len(lines) and re.match(r"^\s+-\s+", lines[i]):
                i += 1
            # 写入新列表
            for sym in symbols:
                out.append(f"  - {sym}")
            replaced = True
            continue
        out.append(line)
        i += 1

    if not replaced:
        raise ValueError("config.yaml 中未找到 symbols: 段, 无法更新")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def update_config_modes(modes: list[str]) -> None:
    """更新 config.yaml 的 modes.enabled 列表 (文本级替换, 保留注释)"""
    path = BASE_DIR / "config.yaml"
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")

    out: list[str] = []
    i = 0
    replaced = False
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^modes:\s*$", line)
        if m and not replaced:
            out.append(line)
            i += 1
            # 跳过旧的 enabled 列表项
            while i < len(lines) and re.match(r"^\s+enabled:\s*\[", lines[i]):
                i += 1
                while i < len(lines) and re.match(r"^\s+-\s+", lines[i]):
                    i += 1
            out.append(f"  enabled: [{', '.join(modes)}]")
            replaced = True
            continue
        out.append(line)
        i += 1

    if not replaced:
        raise ValueError("config.yaml 中未找到 modes: 段, 无法更新")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _replace_block_values(text: str, block_key: str, values: dict) -> str:
    """在 config.yaml 某顶层块内, 按 key 原地替换标量值 (保留注释/顺序/其它 key)

    block_key 为顶层键 (如 "risk" / "leverage")。缩进的子项 `  key: value` 命中 values 时
    被替换; 不在 values 中的子项、注释、空行原样保留。遇到下一个顶层键即退出该块。
    """
    lines = text.split("\n")
    out: list[str] = []
    in_block = False
    for line in lines:
        if re.match(rf"^{block_key}:\s*$", line):
            in_block = True
            out.append(line)
            continue
        if in_block:
            # 下一个顶层键 (非缩进且非注释) → 退出块
            if re.match(r"^\S", line) and not line.startswith("#"):
                in_block = False
                out.append(line)
                continue
            km = re.match(r"^(\s+)(\w[\w_]*):\s*(.*)$", line)
            if km and km.group(2) in values:
                out.append(f"{km.group(1)}{km.group(2)}: {values[km.group(2)]}")
                continue
        out.append(line)
    return "\n".join(out)


def update_config_risk(risk: dict, leverage: dict) -> None:
    """更新 config.yaml 的 risk 与 leverage 块 (文本级替换, 保留注释与顺序)

    Web 设置面板"风控与敞口"保存时调用, 重启后依然生效。
    """
    path = BASE_DIR / "config.yaml"
    text = path.read_text(encoding="utf-8")
    if risk:
        text = _replace_block_values(text, "risk", risk)
    if leverage:
        text = _replace_block_values(text, "leverage", leverage)
    path.write_text(text + "\n", encoding="utf-8")


def update_env_api(key: str, secret: str) -> None:
    """更新 .env 中的测试网 API Key/Secret (保留其他配置与注释)"""
    env_path = BASE_DIR / ".env"
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = text.split("\n")
    out: list[str] = []
    k_set = s_set = False
    for line in lines:
        if line.startswith("BINANCE_TESTNET_API_KEY="):
            out.append(f"BINANCE_TESTNET_API_KEY={key}")
            k_set = True
        elif line.startswith("BINANCE_TESTNET_API_SECRET="):
            out.append(f"BINANCE_TESTNET_API_SECRET={secret}")
            s_set = True
        else:
            out.append(line)
    if not k_set:
        out.append(f"BINANCE_TESTNET_API_KEY={key}")
    if not s_set:
        out.append(f"BINANCE_TESTNET_API_SECRET={secret}")
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")


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
