"""统一的杠杆计算: 所有策略都按信号强度自适应, 受用户预设 [min, max] 与强平安全帽约束。

用户约束 (2026-08-21):
- 所有模式都遵循"强弱逻辑": 信号越强杠杆越高, 不允许任何恒定杠杆。
- 最高杠杆不超过用户预设 leverage.max, 最低不低于 leverage.min。
- 强平安全帽: 止损距离必须 >= 50% 强平距离 (强平距离 ≈ 100% / 杠杆), 否则压低杠杆,
  防止"止损还没到就被插针强平"。
"""
from __future__ import annotations


def compute_leverage(strength: float, sl_pct: float, lev_min: int, lev_max: int) -> int:
    """返回受约束的整型杠杆。

    strength: 信号强度 0~1
        - donchian = 突破深度 (相对 ATR)
        - 单策略   = |score|
        - multi    = |综合评分|
    sl_pct:  止损距离占价格比例 (sl / price), 用于强平安全帽; <=0 时跳过安全帽
    lev_min/lev_max: 用户预设杠杆区间 (顶层 leverage.min / leverage.max)
    """
    lev_min = max(1, int(lev_min))
    lev_max = max(lev_min, int(lev_max))
    s = strength if strength is not None else 0.0
    # 强度 0.15 以下 = 最低杠杆(试探仓), 0.6 以上 = 最高杠杆(重仓), 中间线性
    t = max(0.0, min(1.0, (s - 0.15) / 0.45))
    lev = int(round(lev_min + t * (lev_max - lev_min)))
    lev = max(lev_min, min(lev_max, lev))
    # 强平安全帽: 止损距离 >= 50% 强平距离 (强平距离 ≈ 100% / 杠杆)
    if sl_pct and sl_pct > 0:
        max_safe = max(lev_min, int(0.5 / sl_pct))
        lev = min(lev, max_safe)
    return max(lev_min, lev)
