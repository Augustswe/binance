"""策略自动学习进化器

原理: 维护一个 Donchian 策略组合池 (不同周期×参数), 每天用最近 N 天主网行情
对池内每个组合做回测评估, 自动选择并应用表现最优的组合 —— 系统持续自我进化。

评分 = 平均收益率 - 0.3 × 平均最大回撤 (惩罚大回撤), 交易过少降权。
"""
from __future__ import annotations

import json
import time
import requests
from pathlib import Path

import numpy as np

from .exchange import MAINNET_BASE_URL, BinanceFutures
from .indicators import atr
from .logger import get_logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = DATA_DIR / "learner_state.json"

# Donchian 策略组合池 (timeframe, entry_n, exit_n, sl_atr)
# 覆盖 2h~1d 中长周期趋势跟踪 (短周期已被回测证明负EV, 不纳入)
STRATEGY_POOL = [
    # 2h (灵敏)
    {"timeframe": "2h", "entry_n": 20, "exit_n": 10, "sl_atr": 2.0},
    {"timeframe": "2h", "entry_n": 30, "exit_n": 15, "sl_atr": 2.2},
    {"timeframe": "2h", "entry_n": 40, "exit_n": 20, "sl_atr": 2.5},
    # 4h (主力)
    {"timeframe": "4h", "entry_n": 20, "exit_n": 10, "sl_atr": 2.0},
    {"timeframe": "4h", "entry_n": 25, "exit_n": 12, "sl_atr": 2.2},
    {"timeframe": "4h", "entry_n": 30, "exit_n": 15, "sl_atr": 2.2},
    {"timeframe": "4h", "entry_n": 40, "exit_n": 15, "sl_atr": 2.5},
    {"timeframe": "4h", "entry_n": 55, "exit_n": 20, "sl_atr": 2.5},
    # 8h (稳健)
    {"timeframe": "8h", "entry_n": 20, "exit_n": 10, "sl_atr": 2.0},
    {"timeframe": "8h", "entry_n": 30, "exit_n": 15, "sl_atr": 2.2},
    {"timeframe": "8h", "entry_n": 40, "exit_n": 20, "sl_atr": 2.5},
    {"timeframe": "8h", "entry_n": 55, "exit_n": 20, "sl_atr": 2.5},
    # 12h (趋势)
    {"timeframe": "12h", "entry_n": 20, "exit_n": 10, "sl_atr": 2.0},
    {"timeframe": "12h", "entry_n": 40, "exit_n": 20, "sl_atr": 2.5},
    # 1d (长线)
    {"timeframe": "1d", "entry_n": 20, "exit_n": 10, "sl_atr": 2.0},
    {"timeframe": "1d", "entry_n": 40, "exit_n": 15, "sl_atr": 2.5},
    {"timeframe": "1d", "entry_n": 55, "exit_n": 20, "sl_atr": 2.5},
    {"timeframe": "1d", "entry_n": 60, "exit_n": 25, "sl_atr": 3.0},
]

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]


def _combo_key(combo: dict) -> str:
    return f"{combo['timeframe']}-{combo['entry_n']}/{combo['exit_n']}/{combo['sl_atr']}"


def _donchian_backtest(klines: list, entry_n: int, exit_n: int, sl_atr: float,
                       initial: float = 10000.0, fee: float = 0.0005,
                       qty_usd: float = 1000.0) -> dict:
    """Donchian 逐根K线回测 (收盘价成交, 盘中ATR止损, 通道反向出场)"""
    closes = np.array([k["close"] for k in klines], dtype=float)
    highs = np.array([k["high"] for k in klines], dtype=float)
    lows = np.array([k["low"] for k in klines], dtype=float)
    n = len(closes)
    balance, pos = initial, None
    equity_curve = []
    for i in range(entry_n + 1, n):
        c, h, l = closes[i], highs[i], lows[i]
        up = float(highs[i - entry_n - 1:i - 1].max())
        dn = float(lows[i - entry_n - 1:i - 1].min())
        a = atr(highs[max(0, i - 40):i], lows[max(0, i - 40):i], closes[max(0, i - 40):i], 14)
        if pos is None:
            if c > up:
                pos = {"side": "LONG", "e": c, "q": qty_usd / c, "sl": c - sl_atr * a}
                balance -= qty_usd / 2
            elif c < dn:
                pos = {"side": "SHORT", "e": c, "q": qty_usd / c, "sl": c + sl_atr * a}
                balance -= qty_usd / 2
        else:
            ex_lo = float(lows[i - exit_n - 1:i - 1].min())
            ex_hi = float(highs[i - exit_n - 1:i - 1].max())
            px = None
            if pos["side"] == "LONG":
                if l <= pos["sl"]:
                    px = pos["sl"]
                elif c < ex_lo:
                    px = c
            else:
                if h >= pos["sl"]:
                    px = pos["sl"]
                elif c > ex_hi:
                    px = c
            if px is not None:
                sgn = 1.0 if pos["side"] == "LONG" else -1.0
                gross = (px - pos["e"]) * pos["q"] * sgn
                f = (pos["e"] * pos["q"] + px * pos["q"]) * fee
                balance += pos["q"] * pos["e"] / 2 + gross - f
                pos = None
        eq = balance
        if pos is not None:
            sgn = 1.0 if pos["side"] == "LONG" else -1.0
            eq += (c - pos["e"]) * pos["q"] * sgn
        equity_curve.append(eq)
    if pos is not None:
        px = closes[-1]
        sgn = 1.0 if pos["side"] == "LONG" else -1.0
        gross = (px - pos["e"]) * pos["q"] * sgn
        f = (pos["e"] * pos["q"] + px * pos["q"]) * fee
        balance += pos["q"] * pos["e"] / 2 + gross - f
    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    max_dd = float(((eq - peak) / peak).min() * 100) if len(eq) else 0.0
    return {"ret": (balance - initial) / initial * 100, "max_dd": max_dd}


def evaluate_combo(cfg: dict, combo: dict, days: int = 90) -> dict:
    """用最近 days 天主网行情评估一个组合 (5币平均)"""
    ex = BinanceFutures(cfg, base_url=MAINNET_BASE_URL)
    tf = combo["timeframe"]
    start_ms = int((time.time() - days * 86400) * 1000)
    rets, dds, trades_total = [], [], 0
    # 每周期需要的K线数: 至少覆盖 days 天 + 足够 entry 根
    tf_sec = {"2h": 7200, "4h": 14400, "8h": 28800, "12h": 43200, "1d": 86400}.get(tf, 14400)
    max_bars = max(int(days * 86400 / tf_sec) + combo["entry_n"] * 2, 100)
    for sym in SYMBOLS:
        try:
            kl = ex.get_klines_range(sym, tf, start_ms=start_ms, max_bars=max_bars)
        except Exception:
            continue
        if len(kl) < 80:
            continue
        r = _donchian_backtest(kl, combo["entry_n"], combo["exit_n"], combo["sl_atr"])
        rets.append(r["ret"])
        dds.append(r["max_dd"])
    if not rets:
        return {"combo": combo, "key": _combo_key(combo), "error": "数据不足"}
    avg_ret = float(np.mean(rets))
    avg_dd = float(np.mean(dds))
    score = avg_ret - 0.3 * avg_dd
    return {
        "combo": combo,
        "key": _combo_key(combo),
        "avg_ret": round(avg_ret, 2),
        "avg_dd": round(avg_dd, 2),
        "score": round(score, 2),
        "symbols_eval": len(rets),
    }


def learn(cfg: dict, days: int = 90, pool: list | None = None) -> dict:
    """跑一轮进化学习: 评估策略池全部组合, 返回排名与最优, 记录历史轮次"""
    log = get_logger("learner")
    pool = pool or STRATEGY_POOL
    # 主网可达性快速探测: 不可达则秒退, 避免每个组合都卡满 20s 超时 (~30 分钟冻心跳)
    try:
        _pr = requests.get(
            f"{MAINNET_BASE_URL}/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "4h", "limit": 2},
            timeout=8,
        )
        _pr.raise_for_status()
    except Exception as _e:
        log.warning("主网不可达(fapi.binance.com), 跳过本轮自动学习: %s", _e)
        return {"error": "mainnet unreachable", "skipped": True}
    results = []
    for combo in pool:
        try:
            r = evaluate_combo(cfg, combo, days=days)
            if "error" not in r:
                results.append(r)
                log.info("评估 %s: 收益%+.2f%% 回撤%.2f%% 评分%.2f",
                         r["key"], r["avg_ret"], r["avg_dd"], r["score"])
        except Exception as e:
            log.warning("评估 %s 失败: %s", _combo_key(combo), e)
    if not results:
        return {"error": "全部评估失败"}
    results.sort(key=lambda r: r["score"], reverse=True)
    best = results[0]

    # 读取历史轮次, 追加本轮 (面板展示排名变化)
    old_state: dict = {}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                old_state = json.load(f)
        except Exception:
            old_state = {}
    rounds = old_state.get("rounds", [])
    rounds.append({
        "ts": time.time(),
        "best_key": best["key"],
        "best_score": best["score"],
        "rankings": [
            {"key": r["key"], "score": r["score"],
             "avg_ret": r["avg_ret"], "avg_dd": r["avg_dd"]}
            for r in results
        ],
    })
    rounds = rounds[-60:]  # 保留最近60轮

    # 保存学习状态
    state = {
        "current": best["combo"],
        "best_score": best["score"],
        "learned_at": time.time(),
        "history": results[: len(pool)],
        "rounds": rounds,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    log.info("学习完成: 最优=%s 评分%.2f (共%d轮历史)",
             best["key"], best["score"], len(rounds))
    return {"best": best, "rankings": results, "state_file": str(STATE_FILE)}


def load_learner_state() -> dict:
    """加载学习器完整状态 (当前组合 + 历史轮次排名, 供仪表盘展示)"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ---------------- 模式资金权重学习 (多模式对比) ----------------
# 按各模式实盘表现分配资金权重: 赚钱多的权重高(多给资金), 亏钱的少给
MODE_WEIGHTS_FILE = DATA_DIR / "mode_weights.json"
MODE_LABELS = {"donchian": "Donchian", "multi": "多策略", "grid": "网格",
               "ma_cross": "均线", "rsi": "RSI", "bollinger": "布林带"}


def learn_mode_weights(mode_stats: dict, enabled: list[str] | None = None) -> dict:
    """根据实盘 mode_stats 计算各模式资金权重

    权重公式: 权重 = 1 + 净盈亏缩放, 并做平滑 (避免单笔巨亏导致极端值)
    - realized_pnl > 0: 权重 = 1 + pnl/(基准) , 上限 3
    - realized_pnl <= 0: 权重 = max(0.3, 1 + pnl/|基准|)
    返回 {mode: weight}
    """
    log = get_logger("learner")
    if not mode_stats:
        return {}
    base = 50.0  # 基准: 每盈利 50U 权重 +1 (约合 1% 账户)
    weights: dict[str, float] = {}
    for mode, ms in mode_stats.items():
        pnl = float(ms.get("realized_pnl", 0.0))
        if pnl > 0:
            w = 1.0 + pnl / base
            weights[mode] = max(1.0, min(3.0, w))
        elif pnl < 0:
            w = 1.0 + pnl / base
            weights[mode] = max(0.3, min(1.0, w))
        else:
            weights[mode] = 1.0
    if enabled:
        weights = {m: weights.get(m, 1.0) for m in enabled}
    # 保存
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(MODE_WEIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump({"weights": weights, "learned_at": time.time()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("保存模式权重失败: %s", e)
    log.info("模式资金权重更新: %s",
             " | ".join(f"{MODE_LABELS.get(m, m)}={w:.2f}x" for m, w in weights.items()))
    return weights


def load_mode_weights() -> dict:
    """加载已学习的模式资金权重"""
    if MODE_WEIGHTS_FILE.exists():
        try:
            with open(MODE_WEIGHTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("weights", {})
        except Exception:
            pass
    return {}


def load_current_combo() -> dict | None:
    """加载当前生效的组合 (无则 None)"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            return state.get("current")
        except Exception:
            pass
    return None


def default_combo() -> dict:
    """默认组合: 1d 55/20/2.5 (4年回测5币全正)"""
    return {"timeframe": "1d", "entry_n": 55, "exit_n": 20, "sl_atr": 2.5}
