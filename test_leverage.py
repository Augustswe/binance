"""验证统一杠杆函数 compute_leverage (所有模式强弱逻辑 + 封顶 + 强平安全帽)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))
from strategies.leverage import compute_leverage


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def main():
    # 默认区间 [1, 5], sl_pct 安全 (大止损 -> 无安全帽约束)
    safe = 0.10  # 10% 止损 -> max_safe=5

    # 1) 弱信号 -> 最低杠杆 (试探仓)
    assert compute_leverage(0.0, safe, 1, 5) == 1, "弱信号应=1"
    assert compute_leverage(0.10, safe, 1, 5) == 1, "0.10 仍<0.15 应=1"

    # 2) 强度 0.15 -> 起点; 0.6 -> 满杠杆; 中间线性
    assert compute_leverage(0.15, safe, 1, 5) == 1
    # t=(0.375-0.15)/0.45=0.5 -> lev=1+0.5*4=3
    assert compute_leverage(0.375, safe, 1, 5) == 3, f"0.375 应=3, 实际 {compute_leverage(0.375, safe, 1, 5)}"
    assert compute_leverage(0.60, safe, 1, 5) == 5, "0.60 应=5"
    assert compute_leverage(1.0, safe, 1, 5) == 5, "超强信号封顶=5"

    # 3) 绝不允许恒定杠杆: 同一 [1,5] 区间随强度变化, 不会恒等于某值
    vals = [compute_leverage(s, safe, 1, 5) for s in (0.0, 0.2, 0.4, 0.6, 1.0)]
    assert len(set(vals)) > 1, "杠杆必须随强度变化, 不允许恒定"
    assert set(vals) <= {1, 2, 3, 4, 5}

    # 4) 不超过用户预设 max (即使信号极强)
    assert compute_leverage(1.0, safe, 1, 3) == 3, "max=3 时强信号不能超 3"
    assert compute_leverage(1.0, safe, 2, 4) == 4, "max=4 时封顶 4"
    assert compute_leverage(0.0, safe, 2, 4) == 2, "min=2 时弱信号不低过 2"

    # 5) 强平安全帽: 紧止损时压低杠杆
    # sl_pct=2% -> max_safe = int(0.5/0.02)=25 (不影响, 上限由 max 决定)
    assert compute_leverage(1.0, 0.02, 1, 5) == 5
    # sl_pct=12% -> max_safe = int(0.5/0.12)=4 -> 即使强信号也压到 4
    assert compute_leverage(1.0, 0.12, 1, 5) == 4, f"紧止损应压到4, 实际 {compute_leverage(1.0, 0.12, 1, 5)}"
    # sl_pct=40% -> max_safe = int(1.25)=1 -> 只能 1 倍
    assert compute_leverage(1.0, 0.40, 1, 5) == 1, "极紧止损只能 1x"
    # 安全帽不低于 lev_min
    assert compute_leverage(1.0, 0.90, 1, 5) == 1

    # 6) 单策略 |score| 作 strength 时行为正确 (score 已在 [-1,1])
    assert compute_leverage(abs(-0.5), safe, 1, 5) == compute_leverage(0.5, safe, 1, 5)
    assert compute_leverage(abs(0.9), safe, 1, 5) == 5

    print("PASS test_leverage: 强弱映射 / 封顶 / 强平安全帽 / 无恒定杠杆 全部通过 ✅")


if __name__ == "__main__":
    main()
