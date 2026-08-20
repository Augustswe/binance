"""技术指标计算 (纯 numpy 实现)"""
from __future__ import annotations

import numpy as np


def sma(values, period: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    out = np.full(len(arr), np.nan)
    n = len(arr)
    if n < period or period <= 0:
        return out
    c = np.cumsum(np.insert(arr, 0, 0.0))
    out[period - 1:] = (c[period:] - c[:-period]) / period
    return out


def ema(values, period: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    out = np.full(len(arr), np.nan)
    n = len(arr)
    if n == 0 or period <= 0:
        return out
    alpha = 2.0 / (period + 1)
    out[0] = arr[0]
    for i in range(1, n):
        out[i] = alpha * arr[i] + (1.0 - alpha) * out[i - 1]
    return out


def rsi(values, period: int = 14) -> float:
    """Wilder RSI, 返回最后一个值"""
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    if n < period + 1:
        return float("nan")
    diff = np.diff(arr)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(diff)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def bollinger(values, period: int = 20, num_std: float = 2.0):
    """返回 (中轨, 上轨, 下轨) 的最后一个值"""
    arr = np.asarray(values, dtype=float)
    n = len(arr)
    if n < period:
        return float("nan"), float("nan"), float("nan")
    window = arr[-period:]
    mid = float(window.mean())
    std = float(window.std(ddof=0))
    return mid, mid + num_std * std, mid - num_std * std


def atr_series(highs, lows, closes, period: int = 14) -> np.ndarray:
    """ATR 时间序列 (Wilder 平滑)"""
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows, dtype=float)
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
    )
    out[period] = float(np.mean(tr[:period]))
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i - 1]) / period
    return out


def atr(highs, lows, closes, period: int = 14) -> float:
    s = atr_series(highs, lows, closes, period)
    return float(s[-1]) if not np.isnan(s[-1]) else float("nan")


def trend_strength(closes, fast: int = 8, slow: int = 21) -> float:
    """归一化趋势强度: (EMA快 - EMA慢) / 价格, 正=多头趋势"""
    arr = np.asarray(closes, dtype=float)
    n = len(arr)
    if n < slow + 1:
        return 0.0
    ef = ema(arr, fast)
    es = ema(arr, slow)
    price = float(arr[-1])
    if price <= 0:
        return 0.0
    return float((ef[-1] - es[-1]) / price)


def vol_ratio(atr_values: np.ndarray, lookback: int = 100) -> float:
    """当前ATR与历史均值之比, >1.3 表示波动放大"""
    s = np.asarray(atr_values, dtype=float)
    s = s[~np.isnan(s)]
    if len(s) == 0:
        return 1.0
    cur = float(s[-1])
    hist = s[max(0, len(s) - lookback):-1]
    if len(hist) == 0 or cur <= 0:
        return 1.0
    base = float(np.mean(hist))
    if base <= 0:
        return 1.0
    return cur / base
