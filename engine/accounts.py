"""多账户编排层 (单进程多账户)

- 加载 accounts.yaml, 为每个绑定 API 构建一个独立 TradingEngine 子引擎。
- 每个账户 = 一个测试网 API Key + 一个策略(run_mode), 状态互相隔离
  (data/accounts/<name>/state.json)。
- 无 accounts.yaml / 空列表时退化为单一 "default" 账户, 沿用 config.yaml, 老用户零感知。
"""
from __future__ import annotations

import asyncio
import copy
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from core.config import BASE_DIR
from core.logger import get_logger
from core.state import DATA_DIR

log = get_logger("accounts")

ACCOUNTS_FILE = BASE_DIR / "accounts.yaml"

VALID_MODES = ("auto", "donchian", "multi", "grid", "ma_cross", "rsi", "bollinger")
VALID_NETWORKS = ("testnet", "mainnet")
VALID_APP_MODES = ("paper", "live")

# 策略中文名 (同时用于入口页展示与浏览器标签命名)
STRATEGY_LABELS = {
    "auto": "自动并行",
    "donchian": "唐奇安通道",
    "multi": "多策略自适应",
    "grid": "网格",
    "ma_cross": "均线交叉",
    "rsi": "RSI反转",
    "bollinger": "布林带",
}


@dataclass
class AccountSpec:
    name: str
    enabled: bool = True
    network: str = "testnet"
    mode: str = "live"
    api_key_env: str = ""
    api_secret_env: str = ""
    run_mode: str = "auto"
    symbols: list | None = None
    modes_enabled: list | None = None

    def to_yaml(self) -> dict:
        d = {
            "name": self.name,
            "enabled": self.enabled,
            "network": self.network,
            "mode": self.mode,
            "api_key_env": self.api_key_env,
            "api_secret_env": self.api_secret_env,
            "run_mode": self.run_mode,
        }
        if self.symbols:
            d["symbols"] = list(self.symbols)
        if self.modes_enabled:
            d["modes_enabled"] = list(self.modes_enabled)
        return d


@dataclass
class Account:
    spec: AccountSpec
    engine: Any
    state_file: Path


def load_accounts() -> list[AccountSpec]:
    """解析 accounts.yaml → AccountSpec 列表 (文件缺失/解析失败返回 [])"""
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        log.warning("accounts.yaml 解析失败: %s", e)
        return []
    specs: list[AccountSpec] = []
    for raw in (data.get("accounts") or []):
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        specs.append(AccountSpec(
            name=str(raw["name"]),
            enabled=bool(raw.get("enabled", True)),
            network=raw.get("network", "testnet"),
            mode=raw.get("mode", "live"),
            api_key_env=raw.get("api_key_env", ""),
            api_secret_env=raw.get("api_secret_env", ""),
            run_mode=raw.get("run_mode", "auto"),
            symbols=raw.get("symbols"),
            modes_enabled=raw.get("modes_enabled"),
        ))
    return specs


def save_accounts(specs: list[AccountSpec]) -> None:
    """把账户规格写回 accounts.yaml (保留结构, 不存明文密钥)"""
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    doc = {"accounts": [s.to_yaml() for s in specs]}
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)


def build_account_cfg(global_cfg: dict, spec: AccountSpec) -> dict:
    """全局配置 + 账户覆盖 → 该账户的完整 cfg (供 TradingEngine 使用)

    - api 从 .env 的账户变量名取真实密钥 (明文不落 yaml)
    - run_mode 绑定即固定
    """
    cfg = copy.deepcopy(global_cfg)
    cfg["name"] = spec.name
    cfg["network"] = spec.network
    cfg["mode"] = spec.mode
    cfg["run_mode"] = spec.run_mode if spec.run_mode in VALID_MODES else "auto"
    cfg["api"] = {
        "key": os.getenv(spec.api_key_env, "") if spec.api_key_env else "",
        "secret": os.getenv(spec.api_secret_env, "") if spec.api_secret_env else "",
    }
    if spec.symbols:
        cfg["symbols"] = list(spec.symbols)
    if spec.modes_enabled:
        cfg.setdefault("modes", {})["enabled"] = list(spec.modes_enabled)
    return cfg


class AccountManager:
    def __init__(self, global_cfg: dict):
        self.global_cfg = global_cfg
        self.accounts: list[Account] = []
        self._load()

    # ---------------- 加载 ----------------
    def _default_spec(self) -> AccountSpec:
        g = self.global_cfg
        return AccountSpec(
            name="default",
            enabled=True,
            network=g.get("network", "testnet"),
            mode=g.get("mode", "live"),
            api_key_env="BINANCE_TESTNET_API_KEY",
            api_secret_env="BINANCE_TESTNET_API_SECRET",
            run_mode=g.get("run_mode", "auto"),
        )

    def _state_file_for(self, spec: AccountSpec) -> Path:
        return DATA_DIR / "accounts" / spec.name / "state.json"

    def _build(self, spec: AccountSpec) -> Account:
        from engine.trader import TradingEngine

        cfg = build_account_cfg(self.global_cfg, spec)
        state_file = self._state_file_for(spec)
        # 首次迁移: default 账户若旧 data/state.json 存在且新文件尚未建 → 复制保留历史
        legacy = DATA_DIR / "state.json"
        if spec.name == "default" and not state_file.exists() and legacy.exists():
            state_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, state_file)
            log.info("迁移旧 state.json → %s (原文件保留作备份)", state_file)
        engine = TradingEngine(cfg, state_file=state_file, name=spec.name)
        return Account(spec=spec, engine=engine, state_file=state_file)

    def _load(self) -> None:
        specs = load_accounts()
        if not specs:
            specs = [self._default_spec()]
        self.accounts = [self._build(s) for s in specs if s.enabled]
        if not self.accounts:
            self.accounts = [self._build(self._default_spec())]

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        await asyncio.gather(*[a.engine.start() for a in self.accounts])

    async def stop(self) -> None:
        await asyncio.gather(*[a.engine.stop() for a in self.accounts])

    # ---------------- 查询 ----------------
    def get_engine(self, name: str | None) -> Any | None:
        if name:
            for a in self.accounts:
                if a.spec.name == name:
                    return a.engine
        return self.accounts[0].engine if self.accounts else None

    def get_spec(self, name: str | None) -> AccountSpec | None:
        if name:
            for a in self.accounts:
                if a.spec.name == name:
                    return a.spec
        return self.accounts[0].spec if self.accounts else None

    def list_accounts(self) -> list[dict]:
        return [{
            "name": a.spec.name,
            "strategy": a.engine.run_mode,
            "strategy_label": STRATEGY_LABELS.get(a.engine.run_mode, a.engine.run_mode),
            "network": a.engine.cfg["network"],
            "mode": a.engine.cfg["mode"],
            "enabled": a.spec.enabled,
        } for a in self.accounts]

    def overview(self) -> dict:
        """聚合所有账户盈亏, 供入口页对比总览 (复用各引擎 snapshot)"""
        accs: list[dict] = []
        by_strategy: dict[str, Any] = {}
        tot = {"equity": 0.0, "day_pnl": 0.0, "realized_pnl": 0.0,
               "open_positions": 0, "accounts": 0}
        for a in self.accounts:
            e = a.engine
            try:
                s = e.get_snapshot()
            except Exception:
                s = None
            st = s["strategy_stats"] if s else {}
            row = {
                "name": a.spec.name,
                "strategy": e.run_mode,
                "strategy_label": STRATEGY_LABELS.get(e.run_mode, e.run_mode),
                "network": e.cfg["network"],
                "mode": e.cfg["mode"],
                "running": bool(s["running"]) if s else False,
                "paused": bool(s["paused"]) if s else False,
                "halted": bool(s["halted"]) if s else False,
                "equity": round(s["equity"], 2) if s else 0.0,
                "day_pnl": round(s["day_pnl"], 2) if s else 0.0,
                "realized_pnl": round(st.get("realized_pnl", 0.0), 2),
                "win_rate": st.get("win_rate", 0.0),
                "total_trades": st.get("total_trades", 0),
                "open_positions": len(s["positions"]) if s else 0,
                "api": e.api_status() if hasattr(e, "api_status") else {},
            }
            accs.append(row)
            bs = by_strategy.setdefault(e.run_mode, {
                "accounts": [], "sum_realized": 0.0, "sum_equity": 0.0,
                "strategy_label": STRATEGY_LABELS.get(e.run_mode, e.run_mode),
            })
            bs["accounts"].append(a.spec.name)
            bs["sum_realized"] += row["realized_pnl"]
            bs["sum_equity"] += row["equity"]
            tot["equity"] += row["equity"]
            tot["day_pnl"] += row["day_pnl"]
            tot["realized_pnl"] += row["realized_pnl"]
            tot["open_positions"] += row["open_positions"]
        tot["accounts"] = len(accs)
        return {"accounts": accs, "by_strategy": by_strategy, "totals": tot}

    # ---------------- 热绑定 / 解绑 / 启停 (入口页用, 无需重启) ----------------
    async def bind(self, spec: AccountSpec) -> Account:
        """绑定新账户: 写回 accounts.yaml + 构建引擎 + 启动"""
        if not spec.name or not spec.api_key_env or not spec.api_secret_env:
            raise ValueError("绑定账户需要 name / api_key_env / api_secret_env")
        if spec.run_mode not in VALID_MODES:
            raise ValueError(f"run_mode 非法: {spec.run_mode}")
        if spec.network not in VALID_NETWORKS:
            raise ValueError(f"network 非法: {spec.network}")
        if spec.mode not in VALID_APP_MODES:
            raise ValueError(f"mode 非法: {spec.mode}")
        # 已存在同名 → 先移除旧的
        if any(a.spec.name == spec.name for a in self.accounts):
            await self.unbind(spec.name)
        others = [s for s in load_accounts() if s.name != spec.name]
        others.append(spec)
        save_accounts(others)
        acc = self._build(spec)
        self.accounts.append(acc)
        await acc.engine.start()
        log.info("已绑定账户 %s (策略 %s)", spec.name, spec.run_mode)
        return acc

    async def unbind(self, name: str) -> None:
        """解绑: 停引擎 + 从内存与 yaml 移除 (保留 state 文件备查)"""
        acc = next((a for a in self.accounts if a.spec.name == name), None)
        if acc is not None:
            try:
                await acc.engine.stop()
            except Exception:
                pass
            self.accounts = [a for a in self.accounts if a.spec.name != name]
        others = [s for s in load_accounts() if s.name != name]
        save_accounts(others)
        log.info("已解绑账户 %s", name)

    async def set_enabled(self, name: str, enabled: bool) -> None:
        """启停账户: 更新 yaml + 按 enabled 重建/关停引擎"""
        specs = load_accounts()
        if not specs:
            specs = [self._default_spec()]
        target = next((s for s in specs if s.name == name), None)
        if target is None:
            return
        target.enabled = enabled
        save_accounts(specs)
        if enabled:
            if not any(a.spec.name == name for a in self.accounts):
                acc = self._build(target)
                self.accounts.append(acc)
                await acc.engine.start()
        else:
            acc = next((a for a in self.accounts if a.spec.name == name), None)
            if acc is not None:
                try:
                    await acc.engine.stop()
                except Exception:
                    pass
                self.accounts = [a for a in self.accounts if a.spec.name != name]

    async def reset_account(self, name: str) -> None:
        """重置账户: 清空该账户 state.json (持仓/成交/统计), 引擎重启后从交易所重新同步"""
        acc = next((a for a in self.accounts if a.spec.name == name), None)
        if acc is None:
            return
        try:
            await acc.engine.stop()
        except Exception:
            pass
        sf = acc.state_file
        if sf.exists():
            sf.with_suffix(".json.bak").write_text(
                sf.read_text(encoding="utf-8"), encoding="utf-8")
            sf.unlink()
        log.info("已重置账户 %s (state 已清空, 备份为 .bak)", name)
        new_acc = self._build(acc.spec)
        self.accounts = [a for a in self.accounts if a.spec.name != name]
        self.accounts.append(new_acc)
        await new_acc.engine.start()
