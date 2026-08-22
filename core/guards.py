"""盘中防护模块: 对抗「控盘插针」「闪崩连环爆仓」的四道防线 (纯函数 + 轻量状态机, 便于单测)

四道防线:
  1. VolatilityHalt  (intrabar_halt)   盘中波动率突变熔断: 近 N 秒标的大幅 swing 即暂停开仓
  2. WickFilter      (wick_filter)     插针/影线过滤: 长影线不追开仓 + 止损需收盘确认防扫损
  3. GapProtector    (gap_protect)     跳空穿仓保护: 单笔浮亏超保证金上限即市价减仓 (ATR 软止损的硬兜底)
  4. OIMonitor       (oi_monitor)      资金费率/OI/多空比监控: 捕捉控盘与连环爆仓前兆, 触发避让

所有阈值均来自 config.yaml 的 `guards:` 段, 缺省时回退安全默认值, 不会因配置缺失而崩溃。
纯函数 (candle_wick_ratio / entry_blocked_by_wick / stop_should_trigger / GapProtector.should_hard_stop
/ OIMonitor.check_anomaly) 不依赖网络与状态, 单测可直接喂数据验证。
"""
from __future__ import annotations

from typing import Any


# ---------------- 纯函数: 影线 / 止损确认 ----------------

def candle_wick_ratio(o: float, h: float, l: float, c: float) -> float:
    """蜡烛影线占比: (实体区间 - 实体) / 实体区间; 0=全是实体(无影线), 1=全是影线(十字星)。

    影线占比越高, 说明这根 K 线多空拉锯越剧烈 / 越可能是控盘插针。
    """
    rng = h - l
    if rng <= 0:
        return 0.0
    body = abs(c - o)
    return (rng - body) / rng


def entry_blocked_by_wick(kline: dict, max_wick_ratio: float) -> bool:
    """开仓信号过滤: 触发行若影线占比过高(长影线/插针), 返回 True(应拦截, 不追)。

    kline 需含 open/high/low/close。
    """
    return candle_wick_ratio(
        float(kline["open"]), float(kline["high"]),
        float(kline["low"]), float(kline["close"]),
    ) > max_wick_ratio


def stop_should_trigger(side: str, sl: float, mark: float,
                        candle_low: float, candle_high: float,
                        confirm_on_close: bool) -> bool:
    """止损触发确认 (含影线过滤)。

    - confirm_on_close=False: 直接按标记价穿 SL 触发 (不确认)。
    - confirm_on_close=True : 若当前 K 线只是「插针扫过」SL 又收回 (长影线), 不触发,
      等收盘价真正跌破 SL 才认损 —— 防止控盘长影线把止损扫掉。

    入参 candle_low / candle_high 为「触发时那根 K 线」的最低/最高价 (用于判断是否为影线)。
    """
    if not confirm_on_close:
        return (mark <= sl) if side == "LONG" else (mark >= sl)
    if side == "LONG":
        breached = mark <= sl
        is_wick = (candle_low < sl) and (mark > sl)   # 最低扫过 SL, 但价已拉回 SL 上方
        return breached and not is_wick
    else:
        breached = mark >= sl
        is_wick = (candle_high > sl) and (mark < sl)  # 最高扫过 SL, 但价已拉回 SL 下方
        return breached and not is_wick


# ---------------- 1) 盘中波动率突变熔断 ----------------

class VolatilityHalt:
    """近 lookback 秒内, 任一标的价格 swing(峰-谷)/峰 超过 drawdown_pct 即暂停开仓。

    带迟滞: 触发后需 swing 回落到 resume_pct 以下才恢复开仓, 避免阈值附近反复抖动。
    价格缓冲存在内存(不落盘), 重启后自然从头累积。
    """

    def __init__(self, cfg: dict):
        g = (cfg.get("guards") or {}).get("intrabar_halt", {}) or {}
        self.enabled = bool(g.get("enabled", True))
        self.lookback = float(g.get("lookback_seconds", 300))
        self.drawdown_pct = float(g.get("drawdown_pct", 6)) / 100.0
        self.resume_pct = float(g.get("resume_pct", 3)) / 100.0
        self.buf: dict[str, list[tuple[float, float]]] = {}
        self.halted = False
        self.reason: str | None = None

    def update(self, prices: dict[str, float], now: float) -> None:
        """喂入本轮最新价 (symbol -> price), 仅保留 lookback 窗口内的点。"""
        if not self.enabled:
            return
        cutoff = now - self.lookback
        for sym, p in prices.items():
            if not p or p <= 0:
                continue
            buf = self.buf.setdefault(sym, [])
            buf.append((now, float(p)))
            while buf and buf[0][0] < cutoff:
                buf.pop(0)

    def evaluate(self) -> tuple[bool, str | None]:
        """评估是否暂停开仓, 返回 (暂停?, 原因)。"""
        if not self.enabled:
            self.halted = False
            self.reason = None
            return False, None
        now = max((t for b in self.buf.values() for (t, _) in b), default=0.0)
        worst_sym = None
        worst = 0.0
        for sym, buf in self.buf.items():
            pts = [p for (t, p) in buf if now - t <= self.lookback]
            if len(pts) < 2:
                continue
            peak, trough = max(pts), min(pts)
            swing = (peak - trough) / peak if peak > 0 else 0.0
            if swing > worst:
                worst = swing
                worst_sym = sym
        if worst >= self.drawdown_pct:
            self.halted = True
            self.reason = (
                f"{worst_sym} 近{int(self.lookback)}s 振幅 {worst*100:.1f}% "
                f"> {self.drawdown_pct*100:.1f}%"
            )
        elif self.halted and worst <= self.resume_pct:
            self.halted = False
            self.reason = None
        return self.halted, self.reason


# ---------------- 2) 插针 / 影线过滤 ----------------

class WickFilter:
    """开仓长影线过滤 + 止损收盘确认。具体网络/行情抓取在 engine 中完成, 这里只做判定。"""

    def __init__(self, cfg: dict):
        g = (cfg.get("guards") or {}).get("wick_filter", {}) or {}
        self.enabled = bool(g.get("enabled", True))
        self.max_wick_ratio = float(g.get("max_wick_ratio", 0.7))
        self.confirm_on_close = bool(g.get("confirm_on_close", True))

    def entry_blocked_by_wick(self, kline: dict) -> bool:
        """开仓信号过滤: 触发行长影线则拦截。"""
        if not self.enabled:
            return False
        return entry_blocked_by_wick(kline, self.max_wick_ratio)


# ---------------- 3) 跳空穿仓保护 ----------------

class GapProtector:
    """单笔浮亏超过『保证金』的 max_single_loss_pct% 即市价减仓。

    以保证金为基准(而非名义): 亏损占保证金比例 = 价格跌幅 × 杠杆, 与 ATR 软止损同源,
    因此该硬上限天然位于 ATR 止损之上, 只在「跳空跳过止损 / 行情断流」时兜底, 不会抢在 ATR 前平仓。
    """

    def __init__(self, cfg: dict):
        g = (cfg.get("guards") or {}).get("gap_protect", {}) or {}
        self.enabled = bool(g.get("enabled", True))
        self.max_loss_pct = float(g.get("max_single_loss_pct", 40))

    def should_hard_stop(self, upnl: float, notional: float, leverage: float) -> bool:
        """upnl<0 表示浮亏。返回是否触发硬止损。"""
        if not self.enabled:
            return False
        if notional <= 0 or leverage <= 0:
            return False
        margin = notional / leverage
        if margin <= 0:
            return False
        loss = max(0.0, -float(upnl))
        loss_pct = loss / margin * 100.0
        return loss_pct >= self.max_loss_pct


# ---------------- 4) 资金费率 / OI / 多空比监控 ----------------

class OIMonitor:
    """捕捉控盘与连环爆仓前兆: 资金费率过热 + OI 暴增(杠杆堆砌)。

    每轮(节流)由 engine 拉取各币 funding / OI / 多空比, 调用 check_anomaly 评估;
    任一币异常即置 alert。action=suppress_open 时暂停开仓, action=warn 时仅预警不拦。
    """

    def __init__(self, cfg: dict):
        g = (cfg.get("guards") or {}).get("oi_monitor", {}) or {}
        self.enabled = bool(g.get("enabled", True))
        self.funding_alert = float(g.get("funding_alert", 0.001))
        self.oi_spike_pct = float(g.get("oi_spike_pct", 30))
        self.lookback = float(g.get("lookback_seconds", 300))
        self.interval = float(g.get("interval_seconds", 60))
        self.action = g.get("action", "suppress_open")
        self.oi_prev: dict[str, tuple[float, float]] = {}   # sym -> (ts, oi)
        self.alert = False
        self.reason: str | None = None

    def check_anomaly(self, funding: float, oi_now: float,
                      oi_prev: float | None, ls_ratio: float | None = None) -> tuple[bool, str]:
        """评估单币异常, 返回 (是否异常, 原因)。纯函数, 便于单测。"""
        reasons: list[str] = []
        if abs(float(funding)) >= self.funding_alert:
            reasons.append(f"资金费率 {funding*100:.3f}% 过热")
        if oi_prev and oi_prev > 0:
            chg = (oi_now - oi_prev) / oi_prev * 100.0
            if chg >= self.oi_spike_pct:
                reasons.append(f"OI +{chg:.0f}% 杠杆堆砌")
        return (len(reasons) > 0, "; ".join(reasons))
