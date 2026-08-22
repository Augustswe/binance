"""验证 _strategy_cycle 在多策略(multi)运行模式下, 主导策略必为具体策略名, 不会是元模式 'multi'/'auto'。
同时验证 auto 模式也只开具体策略。
"""
import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

from engine.trader import TradingEngine
from core.guards import WickFilter, GapProtector, VolatilityHalt, OIMonitor
from unittest.mock import AsyncMock

FAKE_KLINES = [
    {"open_time": 0, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "close_time": 0}
    for _ in range(50)
]


class FakeSig:
    def __init__(self, action=None, score=0.0, regime="trend"):
        self.action = action
        self.score = score
        self.regime = regime
        self.strength = 0.8
        self.atr = 100.0
        self.atr_pct = 0.01
        self.sl = 90.0
        self.sl_atr = 1.5
        self.tp_atr = 2.0

    def to_dict(self):
        return {"action": self.action, "score": self.score, "regime": self.regime}


def build_engine(run_mode, signals_by_mode):
    eng = TradingEngine.__new__(TradingEngine)  # 绕过重型 __init__
    eng._last_cycle = {}
    eng.timeframe = "15m"
    eng.kline_limit = 50
    eng.run_mode = run_mode
    eng.enabled_modes = ["donchian", "grid", "ma_cross", "rsi", "bollinger"]
    eng.ml_selector = False
    eng.ml_gate = False
    eng.ml = None
    eng.donchian = None
    eng.ml_threshold = 0.5
    eng._ml_regime = {}
    eng.allow_short = True
    eng.cfg = {"signal": {"close_threshold": 0.0}, "risk": {}}
    # 盘中防护对象 (绕过 __init__ 时需手动补齐; 默认配置下 FAKE_KLINES 全零 -> 不拦截)
    eng.wick_filter = WickFilter(eng.cfg)
    eng.gap_protect = GapProtector(eng.cfg)
    eng.vol_guard = VolatilityHalt(eng.cfg)
    eng.oi_monitor = OIMonitor(eng.cfg)
    eng.exchange = types.SimpleNamespace(
        get_klines=lambda *a, **k: FAKE_KLINES
    )
    state = types.SimpleNamespace(
        data={
            "positions": {},
            "signals": {},
            "mark_prices": {},
            "prices": {"BTCUSDT": 100.0},
        },
        add_event=lambda *a, **k: None,
    )
    eng.state = state
    eng.execution = types.SimpleNamespace(close_position=AsyncMock())
    eng._notify_close = AsyncMock()

    # 真实 _try_open_mode 会经 execution.open_position -> state.open_position 写入 positions,
    # 从而让 "同币种抢到即止" 的 break 生效。这里忠实模拟: 调用即写入持仓并记录调用。
    eng._open_calls = []

    async def _open(mode, symbol, side, mark, sig):
        eng._open_calls.append((mode, symbol, side, mark, sig))
        eng.state.data["positions"][symbol] = {"mode": mode, "side": side}

    eng._try_open_mode = _open
    eng.modes = types.SimpleNamespace(
        analyze=lambda mode, symbol, klines, price=None: signals_by_mode.get(mode)
    )
    return eng


async def test_multi_picks_highest_score():
    # donchian 触发 score=0.5, grid 触发 score=0.9 -> 应挑 grid
    sigs = {
        "donchian": FakeSig("LONG", 0.5),
        "grid": FakeSig("LONG", 0.9),
    }
    eng = build_engine("multi", sigs)
    await eng._strategy_cycle("BTCUSDT")
    calls = eng._open_calls
    assert len(calls) == 1, f"multi 应只开1仓, 实际 {len(calls)} 次: {calls}"
    mode_arg = calls[0][0]
    assert mode_arg == "grid", f"multi 应挑 score 最高的 grid, 实际挑了 {mode_arg!r}"
    assert mode_arg not in ("multi", "auto"), "主导策略绝不可能是元模式"
    print("PASS test_multi_picks_highest_score -> 主导策略 =", mode_arg)


async def test_multi_never_passes_meta():
    # 任意具体策略触发, 都必须以具体策略名开仓, 绝不传 'multi'
    sigs = {"rsi": FakeSig("SHORT", 0.7)}
    eng = build_engine("multi", sigs)
    await eng._strategy_cycle("BTCUSDT")
    calls = eng._open_calls
    assert len(calls) == 1
    assert calls[0][0] == "rsi", f"应为 rsi, 实际 {calls[0][0]!r}"
    print("PASS test_multi_never_passes_meta -> 主导策略 =", calls[0][0])


async def test_auto_opens_concrete_first():
    # auto = 并行, 第一个触发的具体策略开仓 (donchian 在前), 绝不 'multi'
    sigs = {"donchian": FakeSig("LONG", 0.5), "bollinger": FakeSig("LONG", 0.95)}
    eng = build_engine("auto", sigs)
    await eng._strategy_cycle("BTCUSDT")
    calls = eng._open_calls
    assert len(calls) == 1, f"auto 抢到即止应只开1仓, 实际 {len(calls)}"
    assert calls[0][0] == "donchian", f"auto 应由 donchian 抢到, 实际 {calls[0][0]!r}"
    print("PASS test_auto_opens_concrete_first -> 主导策略 =", calls[0][0])


async def test_single_mode_concrete():
    eng = build_engine("grid", {"grid": FakeSig("LONG", 0.6)})
    await eng._strategy_cycle("BTCUSDT")
    calls = eng._open_calls
    assert len(calls) == 1 and calls[0][0] == "grid"
    print("PASS test_single_mode_concrete -> 主导策略 =", calls[0][0])


async def main():
    await test_multi_picks_highest_score()
    await test_multi_never_passes_meta()
    await test_auto_opens_concrete_first()
    await test_single_mode_concrete()
    print("\nALL TESTS PASSED ✅  主导策略在多策略/自动并行/单策略下均为具体策略名")


if __name__ == "__main__":
    asyncio.run(main())
