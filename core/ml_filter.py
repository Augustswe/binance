"""ML 门禁 / 选择器 (纯 numpy, 零外部依赖)

设计目标: 把机器学习当成 Donchian 策略的「门禁」(meta-labeling) 与「选择器」(regime),
而不是去预测价格。全部用 numpy 手写, 不装 scikit-learn。

- 门禁(gate): 给每个 Donchian 入场信号打「这笔交易会盈利」的概率, 低于阈值就不开仓。
  训练标签: 在历史主网K线上重放 Donchian, 每笔入场的后续平仓是否盈利 (1/0)。
- 选择器(selector): 判断当前是趋势市还是震荡市, 震荡市抑制 Donchian 开仓。
  训练标签: 未来 horizon 根K线是否出现 >1ATR 的真实波动 (趋势=1 / 震荡=0)。

防泄漏: 特征只用信号bar及之前的数据; walk-forward 按时间切分并留 embargo 间隔。
"""
from __future__ import annotations

import pickle
import time
from pathlib import Path

import numpy as np

from core.exchange import MAINNET_BASE_URL, BinanceFutures
from core.indicators import atr, atr_series, bollinger, rsi, sma, trend_strength, vol_ratio
from core.logger import get_logger

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
# 训练门禁时用这几组代表性组合重放, 让特征与具体参数解耦 (不只绑死某一组)
TRAIN_COMBOS = [
    {"timeframe": "2h", "entry_n": 20, "exit_n": 10, "sl_atr": 2.0},
    {"timeframe": "4h", "entry_n": 20, "exit_n": 10, "sl_atr": 2.0},
    {"timeframe": "1d", "entry_n": 55, "exit_n": 20, "sl_atr": 2.5},
]
TF_SECONDS = {"2h": 7200, "4h": 14400, "8h": 28800, "12h": 43200, "1d": 86400}
REGIME_HORIZON = 20  # 看未来多少根判断趋势

GATE_FEATURES = ["atr_pct", "rsi", "trend", "sma_slope", "bb_width", "ch_width",
                "vol_ratio", "close_vs_sma50", "ret5", "ret20", "side", "strength"]
REGIME_FEATURES = ["atr_pct", "rsi", "trend", "sma_slope", "bb_width", "ch_width",
                   "vol_ratio", "close_vs_sma50", "ret5", "ret20"]


# ----------------------------- 纯 numpy 逻辑回归 (L2) -----------------------------
class LogReg:
    """手写逻辑回归, L2 正则 + 类别权重, 梯度下降。仅依赖 numpy。"""

    def __init__(self, C: float = 1.0, lr: float = 0.1, iters: int = 500,
                 pos_weight: float = 1.0):
        self.C = C
        self.lr = lr
        self.iters = iters
        self.pos_weight = pos_weight
        self.mean = None
        self.std = None
        self.w = None
        self.b = 0.0

    def _standardize(self, X, fit: bool = False) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        if fit:
            self.mean = X.mean(axis=0)
            self.std = X.std(axis=0) + 1e-9
        return (X - self.mean) / self.std

    def fit(self, X, y):
        Xs = self._standardize(X, fit=True)
        y = np.asarray(y, dtype=float)
        n, d = Xs.shape
        if n == 0:
            return self
        pos = float(y.sum())
        neg = n - pos
        pw = self.pos_weight if pos > 0 else 1.0
        nw = 1.0
        w = np.zeros(d)
        b = 0.0
        decay = 1.0 / self.C  # L2 强度
        for _ in range(self.iters):
            z = Xs @ w + b
            p = 1.0 / (1.0 + np.exp(-z))
            g = (p - y)
            sw = np.where(y > 0.5, pw, nw)
            grad_w = (Xs.T @ (sw * g)) / n + decay * w / n
            grad_b = float(np.mean(sw * g))
            w -= self.lr * grad_w
            b -= self.lr * grad_b
        self.w = w
        self.b = b
        return self

    def predict_proba(self, X) -> np.ndarray:
        if self.w is None:
            return np.array([])
        Xs = self._standardize(X, fit=False)
        z = Xs @ self.w + self.b
        return 1.0 / (1.0 + np.exp(-z))

    def predict(self, X, thr: float = 0.5) -> np.ndarray:
        return (self.predict_proba(X) >= thr).astype(int)


# ----------------------------- 特征工程 -----------------------------
def _base_features(closes, highs, lows, vols, i, a, up_level, dn_level, strength, side):
    """在 bar i 处用「仅过去」数据算基础特征 (gate/regime 共用)。"""
    n = i + 1
    c = float(closes[i])
    atr_pct = (a / c) if c > 0 else 0.0

    r = rsi(closes[:n], 14)
    ts = trend_strength(closes[:n])

    sma20 = sma(closes[:n], 20)
    sma50 = sma(closes[:n], 50)
    if not np.isnan(sma20[-1]) and not np.isnan(sma20[-20]) and sma20[-20] != 0:
        sma_slope = (sma20[-1] - sma20[-20]) / sma20[-20]
    else:
        sma_slope = 0.0

    bb = bollinger(closes[:n], 20, 2.0)
    bb_width = (bb[2] - bb[1]) / bb[0] if (not np.isnan(bb[0]) and bb[0] != 0) else 0.0

    ch_width = (up_level - dn_level) / c if c > 0 else 0.0

    atr_s = atr_series(highs[:n], lows[:n], closes[:n], 14)
    vr = vol_ratio(atr_s, 100)

    close_vs_sma50 = (c / sma50[-1] - 1.0) if (not np.isnan(sma50[-1]) and sma50[-1] != 0) else 0.0
    ret5 = (c / closes[i - 5] - 1.0) if i >= 5 and closes[i - 5] != 0 else 0.0
    ret20 = (c / closes[i - 20] - 1.0) if i >= 20 and closes[i - 20] != 0 else 0.0

    def clean(v, default=0.0):
        return float(v) if not np.isnan(v) else default

    return {
        "atr_pct": clean(atr_pct),
        "rsi": clean(r / 100.0, 0.5),
        "trend": clean(ts),
        "sma_slope": clean(sma_slope),
        "bb_width": clean(bb_width),
        "ch_width": clean(ch_width),
        "vol_ratio": clean(vr, 1.0),
        "close_vs_sma50": clean(close_vs_sma50),
        "ret5": clean(ret5),
        "ret20": clean(ret20),
    }


def _gate_features_vec(closes, highs, lows, vols, i, a, up_level, dn_level, strength, side):
    base = _base_features(closes, highs, lows, vols, i, a, up_level, dn_level, strength, side)
    vec = [base[k] for k in GATE_FEATURES[:10]]
    vec.append(float(side))        # side: +1 LONG / -1 SHORT
    vec.append(float(strength))    # strength 0~1
    return np.array(vec, dtype=float)


def _regime_features_vec(closes, highs, lows, vols, i, a, up_level, dn_level):
    base = _base_features(closes, highs, lows, vols, i, a, up_level, dn_level, 0.0, 0)
    return np.array([base[k] for k in REGIME_FEATURES], dtype=float)


# ----------------------------- 历史回放标注 -----------------------------
def _replay_entries(klines, combo):
    """重放 Donchian, 返回每笔入场的 (特征向量, side, 是否盈利标签)。"""
    closes = np.array([k["close"] for k in klines], dtype=float)
    highs = np.array([k["high"] for k in klines], dtype=float)
    lows = np.array([k["low"] for k in klines], dtype=float)
    vols = np.array([k["volume"] for k in klines], dtype=float)
    n = len(closes)
    entry_n, exit_n, sl_atr = combo["entry_n"], combo["exit_n"], combo["sl_atr"]
    if n < entry_n + entry_n + 5:
        return []
    out = []
    pos = None
    entry_feat = None
    entry_side = 0
    for i in range(entry_n + 1, n):
        c = closes[i]
        up = float(highs[i - entry_n - 1:i - 1].max())
        dn = float(lows[i - entry_n - 1:i - 1].min())
        a = atr(highs[max(0, i - 40):i + 1], lows[max(0, i - 40):i + 1], closes[max(0, i - 40):i + 1], 14)
        if not a or a <= 0:
            a = c * 0.01
        if pos is None:
            if c > up:
                side = 1
                sl = c - sl_atr * a
                strength = max(0.0, min(1.0, (c - up) / (a * 2.0)))
            elif c < dn:
                side = -1
                sl = c + sl_atr * a
                strength = max(0.0, min(1.0, (dn - c) / (a * 2.0)))
            else:
                continue
            pos = {"side": side, "e": c, "sl": sl}
            entry_feat = _gate_features_vec(closes, highs, lows, vols, i, a, up, dn, strength, side)
            entry_side = side
        else:
            ex_lo = float(lows[i - exit_n - 1:i - 1].min())
            ex_hi = float(highs[i - exit_n - 1:i - 1].max())
            px = None
            if pos["side"] == 1:
                if lows[i] <= pos["sl"]:
                    px = pos["sl"]
                elif c < ex_lo:
                    px = c
            else:
                if highs[i] >= pos["sl"]:
                    px = pos["sl"]
                elif c > ex_hi:
                    px = c
            if px is not None:
                pnl = (px - pos["e"]) * pos["side"]
                out.append((entry_feat, entry_side, 1 if pnl > 0 else 0))
                pos = None
    return out


def _replay_regime(klines, horizon: int = REGIME_HORIZON):
    closes = np.array([k["close"] for k in klines], dtype=float)
    highs = np.array([k["high"] for k in klines], dtype=float)
    lows = np.array([k["low"] for k in klines], dtype=float)
    vols = np.array([k["volume"] for k in klines], dtype=float)
    n = len(closes)
    out = []
    for i in range(60, n - horizon):
        c = closes[i]
        if c <= 0:
            continue
        fwd = closes[i + horizon] / c - 1.0
        a_fwd = atr(highs[i:i + horizon + 1], lows[i:i + horizon + 1], closes[i:i + horizon + 1], 14)
        # 趋势市: 未来出现 >0.6ATR 的真实方向性波动 (比原先 1.0ATR 宽松, 避免趋势样本极稀有→模型塌缩)
        trending = 1 if (a_fwd > 0 and abs(fwd) > 0.6 * a_fwd) else 0
        a = atr(highs[max(0, i - 40):i + 1], lows[max(0, i - 40):i + 1], closes[max(0, i - 40):i + 1], 14)
        if not a or a <= 0:
            a = c * 0.01
        up = float(highs[i - 20 - 1:i - 1].max()) if i >= 21 else c
        dn = float(lows[i - 20 - 1:i - 1].min()) if i >= 21 else c
        feat = _regime_features_vec(closes, highs, lows, vols, i, a, up, dn)
        out.append((feat, trending))
    return out


# ----------------------------- walk-forward (防泄漏) -----------------------------
def _roc_auc(y: np.ndarray, p: np.ndarray) -> float:
    y = np.asarray(y, int)
    p = np.asarray(p, float)
    order = np.argsort(p)
    ranks = np.empty(len(p), dtype=float)
    ranks[order] = np.arange(1, len(p) + 1)
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5
    sum_rank = float(ranks[y == 1].sum())
    return (sum_rank - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _balance(X, y, times):
    """下采样多数类到 2× 少数类 (固定随机种子, 可复现), 防止模型在极端不平衡下塌缩为全预测多数类。"""
    X = np.asarray(X, float)
    y = np.asarray(y, int)
    times = np.asarray(times, int)
    pos = y == 1
    neg = y == 0
    n_pos = int(pos.sum())
    n_neg = int(neg.sum())
    if n_pos == 0 or n_neg == 0:
        return X, y, times
    target = min(n_neg, n_pos * 2)
    rng = np.random.default_rng(42)
    neg_idx = rng.choice(np.where(neg)[0], size=target, replace=False)
    keep = np.concatenate([np.where(pos)[0], neg_idx])
    rng.shuffle(keep)
    return X[keep], y[keep], times[keep]


def _walk_forward(X, y, times, K: int = 5, embargo: int = 20, C: float = 1.0):
    """按时间(times)切 K 折, 留 embargo 间隔防重叠; 返回 (oof_preds, oof_y, thr)。"""
    X = np.asarray(X, float)
    y = np.asarray(y, int)
    times = np.asarray(times, int)
    n = len(y)
    if n < 50:
        return np.array([]), np.array([]), 0.5
    order = np.argsort(times)
    folds = np.array_split(order, K)
    oof = np.zeros(n)
    for k in range(K):
        test_idx = folds[k]
        test_start = times[test_idx[0]]
        train_idx = []
        for j in range(K):
            if j == k:
                continue
            f = folds[j]
            # 去掉与测试集太近(embago 内)的训练样本, 防泄漏
            train_idx.append(f[times[f] < test_start - embargo])
        if len(train_idx) == 0:
            continue
        tr = np.concatenate(train_idx)
        if len(tr) < 30 or tr.size == 0:
            continue
        pos = float(y[tr].sum())
        pw = max(0.2, min(5.0, (len(tr) - pos) / pos)) if pos > 0 else 1.0
        m = LogReg(C=C, pos_weight=pw)
        m.fit(X[tr], y[tr])
        oof[test_idx] = m.predict_proba(X[test_idx])
    if oof.sum() == 0:
        return oof, y, 0.5
    # 用 oof 选最优 F1 阈值
    best_th, best_f1 = 0.5, -1.0
    for thr in np.linspace(0.30, 0.70, 21):
        pred = (oof >= thr).astype(int)
        tp = int(((pred == 1) & (y == 1)).sum())
        fp = int(((pred == 1) & (y == 0)).sum())
        fn = int(((pred == 0) & (y == 1)).sum())
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        if f1 > best_f1:
            best_f1, best_th = f1, float(thr)
    return oof, y, best_th


# ----------------------------- 对外 MLFilter -----------------------------
class MLFilter:
    def __init__(self, gate: LogReg | None = None, regime: LogReg | None = None,
                 threshold: float = 0.55, metrics: dict | None = None,
                 trained_at: float = 0.0, regime_threshold: float = 0.5):
        self.gate_model = gate          # LogReg (属性名避开 regime()/gate_prob() 方法, 防同名遮蔽)
        self.regime_model = regime      # LogReg
        self.threshold = threshold
        self.regime_threshold = regime_threshold
        self.metrics = metrics or {}
        self.trained_at = trained_at

    @classmethod
    def train(cls, cfg: dict, days: int = 365):
        log = get_logger("ml_filter")
        ex = BinanceFutures(cfg, base_url=MAINNET_BASE_URL)
        mlcfg = cfg.get("ml_filter", {})
        days = int(mlcfg.get("days", days))

        # ---- 1. 门禁数据 ----
        gate_rows, gate_y, gate_t = [], [], []
        for sym in SYMBOLS:
            for combo in TRAIN_COMBOS:
                tf = combo["timeframe"]
                max_bars = max(int(days * 86400 / TF_SECONDS.get(tf, 14400)) + combo["entry_n"] * 2, 200)
                try:
                    kl = ex.get_klines_range(sym, tf, max_bars=max_bars)
                except Exception as e:
                    log.warning("门禁拉取 %s %s 失败: %s", sym, tf, e)
                    continue
                if len(kl) < 120:
                    continue
                for feat, side, label in _replay_entries(kl, combo):
                    gate_rows.append(feat)
                    gate_y.append(label)
                    gate_t.append(len(gate_t))  # 近似时间序 (同币种内有序)
        # ---- 2. 选择器数据 ----
        reg_rows, reg_y, reg_t = [], [], []
        for sym in SYMBOLS:
            tf = "4h"
            max_bars = max(int(days * 86400 / TF_SECONDS.get(tf, 14400)) + 80, 200)
            try:
                kl = ex.get_klines_range(sym, tf, max_bars=max_bars)
            except Exception as e:
                log.warning("选择器拉取 %s 失败: %s", sym, e)
                continue
            if len(kl) < 120:
                continue
            for feat, label in _replay_regime(kl):
                reg_rows.append(feat)
                reg_y.append(label)
                reg_t.append(len(reg_t))

        if len(gate_rows) < 50:
            log.error("门禁样本不足(%d), 训练中止", len(gate_rows))
            raise RuntimeError("门禁样本不足, 无法训练")
        if len(reg_rows) < 50:
            log.warning("选择器样本不足(%d), 选择器将不可用", len(reg_rows))
            regime_model = None
            reg_metrics = {}
        else:
            Xr, yr, tr = _balance(np.array(reg_rows), np.array(reg_y), np.array(reg_t))
            reg_metrics = cls._fit_one(Xr, yr, tr, "selector")
            regime_model = LogReg(C=1.0, pos_weight=reg_metrics.get("pos_weight", 1.0))
            regime_model.fit(Xr, yr)

        Xg, yg, tg = _balance(np.array(gate_rows), np.array(gate_y), np.array(gate_t))
        gate_metrics = cls._fit_one(Xg, yg, tg, "gate")
        gate_model = LogReg(C=1.0, pos_weight=gate_metrics.get("pos_weight", 1.0))
        gate_model.fit(Xg, yg)

        log.info("ML 训练完成: 门禁样本=%d AUC=%.3f 选择器样本=%d AUC=%.3f",
                 len(gate_rows), gate_metrics.get("auc", 0.0),
                 len(reg_rows), reg_metrics.get("auc", 0.0))
        reg_thr = float(reg_metrics.get("threshold", 0.5)) if regime_model else 0.5
        return cls(gate=gate_model, regime=regime_model,
                   threshold=float(gate_metrics.get("threshold", 0.55)),
                   regime_threshold=reg_thr,
                   metrics={"gate": gate_metrics, "regime": reg_metrics,
                            "gate_samples": len(gate_rows), "regime_samples": len(reg_rows),
                            "days": days, "trained_at": time.time()},
                   trained_at=time.time())

    @staticmethod
    def _fit_one(X, y, times, name):
        oof, yv, thr = _walk_forward(X, y, times)
        if oof.sum() == 0:
            return {"auc": 0.5, "threshold": 0.55, "pos_weight": 1.0,
                    "precision": 0.0, "recall": 0.0, "win_rate_allowed": 0.0,
                    "firing_rate": 0.0}
        # 选阈值使预测为正(趋势/允许)的占比 ≈ firing_rate, 避免极端不平衡下模型塌缩为全负(selector 原 bug)
        firing_rate = 0.40
        thr = float(np.quantile(oof, 1.0 - firing_rate))
        pred = (oof >= thr).astype(int)
        tp = int(((pred == 1) & (yv == 1)).sum())
        fp = int(((pred == 1) & (yv == 0)).sum())
        fn = int(((pred == 0) & (yv == 1)).sum())
        tn = int(((pred == 0) & (yv == 0)).sum())
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        allowed = yv[pred == 1]
        win_allowed = float(allowed.mean()) if len(allowed) else 0.0
        pos = float(yv.sum())
        pw = max(0.2, min(5.0, (len(yv) - pos) / pos)) if pos > 0 else 1.0
        return {"auc": round(float(_roc_auc(yv, oof)), 4),
                "threshold": round(float(thr), 3),
                "precision": round(prec, 4), "recall": round(rec, 4),
                "firing_rate": round(float(pred.mean()), 4),
                "win_rate_allowed": round(win_allowed, 4),
                "pos_weight": round(float(pw), 3),
                "n_allowed": int(pred.sum()), "n_total": int(len(yv))}

    # ---- 实时推理 ----
    def features_for_gate(self, klines, sig, donchian_engine) -> np.ndarray | None:
        if self.gate_model is None:
            return None
        closes = np.array([k["close"] for k in klines], dtype=float)
        highs = np.array([k["high"] for k in klines], dtype=float)
        lows = np.array([k["low"] for k in klines], dtype=float)
        vols = np.array([k["volume"] for k in klines], dtype=float)
        i = len(closes) - 1
        if i < 60:
            return None
        c = closes[i]
        a = atr(highs[-40:], lows[-40:], closes[-40:], 14)
        if not a or a <= 0:
            a = c * 0.01
        up = float(sig.up_level)
        dn = float(sig.dn_level)
        side = 1 if sig.action == "LONG" else (-1 if sig.action == "SHORT" else 0)
        strength = float(getattr(sig, "strength", 0.0))
        return _gate_features_vec(closes, highs, lows, vols, i, a, up, dn, strength, side)

    def gate_prob(self, features) -> float:
        if self.gate_model is None or features is None:
            return 1.0
        p = self.gate_model.predict_proba(np.asarray(features, float).reshape(1, -1))
        return float(p[0]) if len(p) else 1.0

    def regime(self, klines) -> str:
        if self.regime_model is None:
            return "trend"  # 无选择器模型时默认趋势市(不抑制)
        closes = np.array([k["close"] for k in klines], dtype=float)
        highs = np.array([k["high"] for k in klines], dtype=float)
        lows = np.array([k["low"] for k in klines], dtype=float)
        vols = np.array([k["volume"] for k in klines], dtype=float)
        i = len(closes) - 1
        if i < 60:
            return "trend"
        c = closes[i]
        a = atr(highs[-40:], lows[-40:], closes[-40:], 14)
        if not a or a <= 0:
            a = c * 0.01
        up = float(highs[i - 20 - 1:i - 1].max()) if i >= 21 else c
        dn = float(lows[i - 20 - 1:i - 1].min()) if i >= 21 else c
        feat = _regime_features_vec(closes, highs, lows, vols, i, a, up, dn)
        p = self.regime_model.predict_proba(feat.reshape(1, -1))
        return "trend" if (p[0] if len(p) else 0.5) >= self.regime_threshold else "range"

    # ---- 持久化 ----
    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({
                "gate": self.gate_model, "regime": self.regime_model,
                "threshold": self.threshold, "regime_threshold": self.regime_threshold,
                "metrics": self.metrics,
                "trained_at": self.trained_at,
                "gate_features": GATE_FEATURES, "regime_features": REGIME_FEATURES,
            }, f)

    @classmethod
    def load(cls, path: str) -> "MLFilter | None":
        p = Path(path)
        if not p.exists():
            return None
        try:
            with open(p, "rb") as f:
                d = pickle.load(f)
            return cls(gate=d.get("gate"), regime=d.get("regime"),
                       threshold=float(d.get("threshold", 0.55)),
                       regime_threshold=float(d.get("regime_threshold", 0.5)),
                       metrics=d.get("metrics"), trained_at=float(d.get("trained_at", 0.0)))
        except Exception as e:
            get_logger("ml_filter").warning("ml_filter 模型加载失败: %s", e)
            return None
