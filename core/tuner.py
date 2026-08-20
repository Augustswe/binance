"""AI 自动调参: 用回测结果作为评估函数, 随机搜索 + 爬山优化策略参数

- 参数空间: 开平仓阈值 / ATR止盈止损 / 网格间距 / RSI阈值 / 均线周期 / 布林带参数
- 评估: 年化Sharpe为主, 惩罚低盈亏比和大回撤
- 只在"显著优于当前参数"时应用, 防止过拟合毛刺
"""
from __future__ import annotations

import copy
import json
import random
import time
from pathlib import Path

from .backtest import Backtester
from .exchange import MAINNET_BASE_URL, BinanceFutures
from .logger import get_logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TUNED_FILE = DATA_DIR / "tuned_params.json"

# 参数空间: (path..., (min, max)) 或 (path..., (min, max, int))
# 注意1: 用户选择激进模式, 开仓阈值锁定在 0.12~0.25, 防止调参器把门槛调高
# 注意2: close_threshold 固定为 0 (禁用"信号消失"平仓), 不参与优化
PARAM_SPACE = [
    (("signal", "open_threshold"), (0.12, 0.25)),
    (("position", "tp_atr"), (1.0, 4.0)),
    (("position", "sl_atr"), (0.8, 3.0)),
    (("strategies", "grid", "grid_pct"), (0.008, 0.05)),
    (("strategies", "grid", "levels"), (5, 20, int)),
    (("strategies", "rsi", "oversold"), (20, 40, int)),
    (("strategies", "rsi", "overbought"), (60, 80, int)),
    (("strategies", "ma_cross", "fast"), (5, 15, int)),
    (("strategies", "ma_cross", "slow"), (15, 40, int)),
    (("strategies", "bollinger", "period"), (15, 30, int)),
    (("strategies", "bollinger", "std"), (1.5, 3.0)),
]

MIN_TRADES = 10
IMPROVE_THRESHOLD = 0.15  # 评分提升超过此值才应用


def _score(result) -> float:
    st = result.stats()
    if st["trades"] < MIN_TRADES:
        return -999.0
    pf = st["profit_factor"] if st["profit_factor"] != float("inf") else 10.0
    score = st["sharpe"]
    if pf < 1.0:
        score -= (1.0 - pf) * 3.0
    if st["max_drawdown_pct"] > 30.0:
        score -= (st["max_drawdown_pct"] - 30.0) / 10.0
    return score


def _sample_param(rng: random.Random, path, bounds) -> float:
    if len(bounds) == 3 and bounds[2] is int:
        return rng.randint(int(bounds[0]), int(bounds[1]))
    return rng.uniform(bounds[0], bounds[1])


def _get(cfg, path):
    node = cfg
    for k in path:
        node = node.get(k)
        if node is None:
            return None
    return node


def _set(cfg, path, value):
    node = cfg
    for k in path[:-1]:
        node = node.setdefault(k, {})
    node[path[-1]] = value


def _apply_params(cfg, params: dict):
    for path, value in params.items():
        _set(cfg, tuple(path.split(".")), value)


def run_tuning(cfg: dict, symbol: str = "BTCUSDT", timeframe: str = "5m",
               bars: int = 2000, trials: int = 20, seed: int | None = None,
               log_progress: bool = True) -> dict:
    """跑一轮参数优化, 返回结果 dict"""
    log = get_logger("tuner")
    t0 = time.time()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1) 拉取主网历史数据
    ex = BinanceFutures(cfg, base_url=MAINNET_BASE_URL)
    klines = ex.get_klines_range(symbol, timeframe, max_bars=bars)
    if len(klines) < 300:
        log.error("历史数据不足: %d 根", len(klines))
        return {"improved": False, "error": "历史数据不足"}
    min_qty, step_size = ex.lot_size(symbol)
    log.info("拉取 %s %s 历史K线 %d 根 (minQty=%s step=%s)",
             symbol, timeframe, len(klines), min_qty, step_size)

    def make_bt(cfg_used) -> Backtester:
        return Backtester(cfg_used, symbol, timeframe, min_qty=min_qty, step_size=step_size)

    # 2) 基线评分 (当前参数)
    base_cfg = copy.deepcopy(cfg)
    base_result = make_bt(base_cfg).run(klines)
    base_score = _score(base_result)
    log.info("基线: score=%.2f trades=%d sharpe=%.2f 收益=%.2f%%",
             base_score, base_result.stats()["trades"], base_result.stats()["sharpe"],
             base_result.stats()["total_return_pct"])

    # 3) 随机搜索
    rng = random.Random(seed)
    best_cfg = copy.deepcopy(cfg)
    best_score = base_score
    best_params: dict = {}
    for t in range(trials):
        trial = copy.deepcopy(cfg)
        params: dict = {}
        for path, bounds in PARAM_SPACE:
            v = _sample_param(rng, path, bounds)
            _set(trial, path, v)
            params[".".join(path)] = v
        result = make_bt(trial).run(klines)
        score = _score(result)
        if log_progress and (t + 1) % 5 == 0:
            log.info("随机搜索 %d/%d: score=%.2f (当前最优 %.2f)",
                     t + 1, trials, score, max(best_score, base_score))
        if score > best_score:
            best_score = score
            best_cfg = trial
            best_params = params

    # 4) 爬山: 从最优解邻域微调
    for _ in range(12):
        trial = copy.deepcopy(best_cfg)
        params = dict(best_params)
        for path, bounds in rng.sample(PARAM_SPACE, min(3, len(PARAM_SPACE))):
            cur = _get(trial, path)
            if cur is None:
                continue
            delta = (bounds[1] - bounds[0]) * rng.uniform(-0.15, 0.15)
            new_v = cur + delta
            if len(bounds) == 3 and bounds[2] is int:
                new_v = int(round(new_v))
            new_v = max(bounds[0], min(bounds[1], new_v))
            _set(trial, path, new_v)
            params[".".join(path)] = new_v
        result = make_bt(trial).run(klines)
        score = _score(result)
        if score > best_score:
            best_score = score
            best_cfg = trial
            best_params = params

    improved = best_score > base_score + IMPROVE_THRESHOLD
    log.info("优化完成: 耗时%.0fs | 基线 %.2f -> 最优 %.2f | 改进: %s",
             time.time() - t0, base_score, best_score, improved)
    if improved:
        best_stats = make_bt(best_cfg).run(klines).stats()
        log.info("最优参数: %s", best_params)
        log.info("最优绩效: trades=%d sharpe=%.2f 收益=%.2f%% 回撤=%.2f%%",
                 best_stats["trades"], best_stats["sharpe"],
                 best_stats["total_return_pct"], best_stats["max_drawdown_pct"])

    return {
        "improved": improved,
        "symbol": symbol,
        "timeframe": timeframe,
        "bars": len(klines),
        "score": round(best_score, 3),
        "base_score": round(base_score, 3),
        "base_stats": base_result.stats(),
        "params": best_params,
        "ts": time.time(),
        "elapsed_sec": round(time.time() - t0, 1),
    }


def save_tuned_params(result: dict) -> Path | None:
    """把最优参数写入 data/tuned_params.json (配置加载时自动合并)"""
    if not result.get("improved") or not result.get("params"):
        return None
    params = result["params"]
    out = {
        "signal": {},
        "position": {},
        "strategies": {},
    }
    for path, value in params.items():
        parts = path.split(".")
        if parts[0] == "signal":
            out["signal"][parts[1]] = value
        elif parts[0] == "position":
            out["position"][parts[1]] = value
        elif parts[0] == "strategies" and len(parts) == 3:
            out["strategies"].setdefault(parts[1], {})[parts[2]] = value
    out["meta"] = {
        "score": result["score"], "base_score": result["base_score"],
        "symbol": result.get("symbol"), "timeframe": result.get("timeframe"),
        "bars": result.get("bars"), "ts": result["ts"],
    }
    TUNED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TUNED_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return TUNED_FILE


def load_tuned_params() -> dict:
    if TUNED_FILE.exists():
        try:
            with open(TUNED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}
