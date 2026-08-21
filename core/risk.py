"""风控模块: 单笔限额、总持仓限额、持仓数上限、日亏损熔断"""
from __future__ import annotations

import time

from .logger import get_logger


def fmt_cooldown(sec: int) -> str:
    """把剩余秒数格式化为倒计时文本: MM:SS 或超过 1 小时则 H:MM:SS"""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class RiskManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.risk = cfg["risk"]
        self.log = get_logger("risk")

    def check_open(self, state, symbol: str, notional: float) -> tuple[bool, str]:
        """开仓前的风控检查, 返回 (是否允许, 拒绝原因)"""
        d = state.data
        if d["paused"]:
            return False, "系统已暂停"
        if d["halted"]:
            return False, f"熔断中: {d['halt_reason']}"
        if notional <= 0:
            return False, "名义价值<=0"
        if notional > self.risk["max_single_order_notional"]:
            return False, f"单笔{notional:.0f}U 超过上限 {self.risk['max_single_order_notional']}U"
        if state.exposure() + notional > self.risk["max_total_position_notional"]:
            return False, "总持仓超过上限"
        # 总持仓敞口占权益上限 (max_total_exposure_pct, 0=关闭)
        pct = self.risk.get("max_total_exposure_pct", 0) or 0
        if pct > 0:
            eq = state.equity()
            if eq > 0:
                projected = state.exposure() + notional
                if projected > eq * pct / 100.0:
                    return False, (
                        f"总敞口占权益 {projected / eq * 100:.0f}% "
                        f"超过上限 {pct:.0f}%"
                    )
        if len(d["positions"]) >= self.risk["max_positions"]:
            return False, "持仓数量达到上限"
        # 日亏损熔断
        equity = state.equity()
        day_start = d["day_start_equity"]
        if day_start > 0:
            dd = (equity - day_start) / day_start
            if dd <= -self.risk["daily_loss_stop"]:
                state.halt(f"日亏损 {abs(dd)*100:.1f}% 触发熔断")
                return False, "日亏损熔断"
        # 冷静期
        last_close = d["last_close_time"].get(symbol, 0)
        cooldown = self.risk["cooldown_minutes"] * 60
        if last_close:
            elapsed = time.time() - last_close
            if elapsed < cooldown:
                remaining = int(cooldown - elapsed)
                return False, f"冷静期 {fmt_cooldown(remaining)}"
        return True, "ok"

    def should_halt(self, state) -> bool:
        """检查是否触发日亏损熔断"""
        d = state.data
        if d["halted"]:
            return True
        day_start = d["day_start_equity"]
        if day_start <= 0:
            return False
        dd = (state.equity() - day_start) / day_start
        if dd <= -self.risk["daily_loss_stop"]:
            state.halt(f"日亏损 {abs(dd)*100:.1f}% 触发熔断")
            return True
        return False
