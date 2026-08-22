"""四道盘中防护的单测: 波动率熔断 / 影线过滤 / 跳空穿仓 / OI监控 + check_open 拦截集成。

纯函数直接喂数据; 集成用例用真实 TradingState + RiskManager 验证开仓拦截。
运行: .venv/bin/python3 test_guards.py   (也可 pytest)
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

from core.guards import (
    candle_wick_ratio, entry_blocked_by_wick, stop_should_trigger,
    VolatilityHalt, WickFilter, GapProtector, OIMonitor,
)
from core.risk import RiskManager
from core.state import TradingState


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def make_cfg(guards: dict | None = None) -> dict:
    return {
        "mode": "paper",
        "paper_initial_balance": 10000,
        "risk": {
            "max_single_order_notional": 1000,
            "max_total_position_notional": 2000,
            "margin_per_position": 200,
            "daily_loss_stop": 0.3,
            "max_positions": 5,
            "cooldown_minutes": 5,
            "max_total_exposure_pct": 40,
            "enforce_exposure_cap": "reduce",
        },
        "guards": guards or {},
    }


def test_candle_wick_ratio():
    # 全是实体 (high=close, low=open) -> 0
    assert approx(candle_wick_ratio(100, 109, 100, 109), 0.0)
    # 十字星(无实体) -> 1
    assert approx(candle_wick_ratio(100, 110, 90, 100), 1.0)
    # open=100, close=100, high=110, low=90 -> range=20 body=0 -> 1.0
    assert approx(candle_wick_ratio(100, 110, 90, 100), 1.0)
    # open=100, close=109, high=110, low=99 -> range=11 body=9 -> 2/11
    assert approx(candle_wick_ratio(100, 110, 99, 109), 2 / 11)
    # 零区间 -> 0
    assert approx(candle_wick_ratio(100, 100, 100, 100), 0.0)


def test_entry_blocked_by_wick():
    kl = {"open": 100, "high": 110, "low": 90, "close": 100}
    assert entry_blocked_by_wick(kl, 0.7) is True     # ratio=1.0 > 0.7
    kl2 = {"open": 100, "high": 110, "low": 99, "close": 109}
    assert entry_blocked_by_wick(kl2, 0.7) is False   # ratio=0.18
    # 阈值边界: ratio 恰好 0.7 不拦截(严格 >)
    assert entry_blocked_by_wick({"open": 100, "high": 105, "low": 95, "close": 103}, 0.7) is False


def test_stop_should_trigger():
    # confirm_on_close=False: 直接按标记价
    assert stop_should_trigger("LONG", 100, 98, 95, 110, False) is True
    assert stop_should_trigger("LONG", 100, 101, 95, 110, False) is False
    # confirm_on_close=True, LONG: 标记价已回到 SL 上方 -> 长影线, 不触发
    assert stop_should_trigger("LONG", 100, 101, 95, 110, True) is False
    # confirm_on_close=True, LONG: 标记价仍在 SL 下方 -> 真跌破, 触发
    assert stop_should_trigger("LONG", 100, 98, 95, 110, True) is True
    # confirm_on_close=True, LONG: 标记价 <= SL 但最低未扫过 SL -> 真跌破, 触发
    assert stop_should_trigger("LONG", 100, 99, 101, 110, True) is True
    # SHORT 对称
    assert stop_should_trigger("SHORT", 100, 102, 95, 105, True) is True   # 标记>=SL, 最高扫过
    assert stop_should_trigger("SHORT", 100, 99, 95, 105, True) is False  # 未跌破(最高扫过SL又收回=影线, 不触发)


def test_volatility_halt_trigger_and_resume():
    cfg = make_cfg({"intrabar_halt": {"enabled": True, "lookback_seconds": 100,
                                       "drawdown_pct": 5, "resume_pct": 2}})
    vh = VolatilityHalt(cfg)
    vh.update({"BTC": 100.0}, 1000)
    vh.update({"BTC": 100.0}, 1010)
    vh.update({"BTC": 90.0}, 1020)          # 振幅 (100-90)/100 = 10% > 5%
    halted, reason = vh.evaluate()
    assert halted, "10% 振幅应触发熔断"
    assert "10.0%" in reason
    # 越过 lookback, 价格回稳 -> 振幅回落到 resume_pct 以下, 解除
    vh.update({"BTC": 100.0}, 1130)         # 90 的点(1020)已超出 100s 窗口
    vh.update({"BTC": 100.0}, 1140)
    halted2, _ = vh.evaluate()
    assert not halted2, "回稳后应解除熔断"


def test_volatility_halt_disabled():
    vh = VolatilityHalt(make_cfg({"intrabar_halt": {"enabled": False}}))
    vh.update({"BTC": 100.0}, 1000)
    vh.update({"BTC": 50.0}, 1010)          # 50% 振幅
    halted, _ = vh.evaluate()
    assert not halted


def test_wick_filter_entry():
    cfg = make_cfg({"wick_filter": {"enabled": True, "max_wick_ratio": 0.7, "confirm_on_close": True}})
    wf = WickFilter(cfg)
    assert wf.entry_blocked_by_wick({"open": 100, "high": 110, "low": 90, "close": 100})
    assert not wf.entry_blocked_by_wick({"open": 100, "high": 110, "low": 99, "close": 109})
    # 关闭后不拦截
    wf_off = WickFilter(make_cfg({"wick_filter": {"enabled": False}}))
    assert not wf_off.entry_blocked_by_wick({"open": 100, "high": 110, "low": 90, "close": 100})


def test_gap_protector():
    cfg = make_cfg({"gap_protect": {"enabled": True, "max_single_loss_pct": 40}})
    gp = GapProtector(cfg)
    # 浮亏占保证金 50% (notional=1000, lev=5 -> margin=200, loss=100) -> 触发
    assert gp.should_hard_stop(-100, 1000, 5) is True
    # 浮亏占保证金 10% -> 不触发
    assert gp.should_hard_stop(-20, 1000, 5) is False
    # ATR 软止损在保证金基准下应低于阈值(不抢先平仓):
    # 价格跌 2.7*2%=5.4% -> 保证金亏 5.4%*5=27% < 40% -> 不触发
    assert gp.should_hard_stop(-(0.054 * 1000), 1000, 5) is False
    # 关闭后永不触发
    assert GapProtector(make_cfg({"gap_protect": {"enabled": False}})).should_hard_stop(-100, 1000, 5) is False
    # 非法输入保护
    assert gp.should_hard_stop(0, 0, 0) is False


def test_oi_monitor_anomaly():
    cfg = make_cfg({"oi_monitor": {"enabled": True, "funding_alert": 0.001,
                                    "oi_spike_pct": 30, "action": "suppress_open"}})
    oi = OIMonitor(cfg)
    # 资金费率过热 + OI 暴增
    alert, reason = oi.check_anomaly(0.0015, 130, 100)
    assert alert and "资金费率" in reason and "OI" in reason
    # 都正常
    assert oi.check_anomaly(0.0002, 105, 100)[0] is False
    # 仅 OI 暴增
    alert3, reason3 = oi.check_anomaly(0.0002, 140, 100)
    assert alert3 and "OI" in reason3 and "资金费率" not in reason3
    # 首次无基准 -> 只看资金费率
    assert oi.check_anomaly(0.0015, 130, None)[0] is True
    # 负资金费率(空头过热)也应捕捉
    assert oi.check_anomaly(-0.0015, 100, 100)[0] is True


def test_check_open_respects_guards():
    cfg = make_cfg()
    tmp = Path(tempfile.mktemp(suffix=".json"))
    state = TradingState(cfg, state_file=tmp)
    rm = RiskManager(cfg)
    # 正常允许
    ok, r = rm.check_open(state, "BTCUSDT", 500)
    assert ok, f"正常应允许开仓, 实际: {r}"
    # 盘中波动率熔断拦截
    state.data["vol_halt"] = True
    state.data["vol_halt_reason"] = "BTC 近300s 振幅 8.0% > 6.0%"
    ok2, r2 = rm.check_open(state, "BTCUSDT", 500)
    assert not ok2 and "盘中波动率熔断" in r2, f"应被波动率熔断拦, 实际: {r2}"
    state.data["vol_halt"] = False
    # OI 异常避让拦截
    state.data["oi_alert_suppress"] = True
    state.data["oi_alert_reason"] = "BTC: 资金费率 0.150% 过热"
    ok3, r3 = rm.check_open(state, "BTCUSDT", 500)
    assert not ok3 and "OI/资金费率" in r3, f"应被 OI 避让拦, 实际: {r3}"


if __name__ == "__main__":
    test_candle_wick_ratio()
    test_entry_blocked_by_wick()
    test_stop_should_trigger()
    test_volatility_halt_trigger_and_resume()
    test_volatility_halt_disabled()
    test_wick_filter_entry()
    test_gap_protector()
    test_oi_monitor_anomaly()
    test_check_open_respects_guards()
    print("PASS test_guards: 四道盘中防护 + check_open 拦截 全部通过 ✅")
