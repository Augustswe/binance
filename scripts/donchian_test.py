"""Donchian 通道突破趋势跟踪策略验证 (海龟风格)"""
import numpy as np
from core.config import load_config
from core.exchange import BinanceFutures, MAINNET_BASE_URL
from core.indicators import atr


def donchian_backtest(klines, entry_n, exit_n, sl_atr, initial=10000.0, fee=0.0005):
    """入场: 突破前entry_n根最高→做多 / 跌破前entry_n根最低→做空
    出场: 反向突破前exit_n根通道平仓 (让利润跑) + ATR止损"""
    closes = np.array([k['close'] for k in klines])
    highs = np.array([k['high'] for k in klines])
    lows = np.array([k['low'] for k in klines])
    n = len(closes)
    balance = initial
    pos = None
    equity_curve, trades = [], []
    for i in range(entry_n + 1, n):
        c, h, l = closes[i], highs[i], lows[i]
        up_level = float(highs[i - entry_n - 1:i - 1].max())
        dn_level = float(lows[i - entry_n - 1:i - 1].min())
        a = atr(highs[max(0, i - 30):i], lows[max(0, i - 30):i], closes[max(0, i - 30):i], 14)
        if pos is None:
            if c > up_level:
                qty = 200.0 / c
                pos = {'side': 'LONG', 'entry': c, 'qty': qty, 'sl': c - sl_atr * a}
                balance -= qty * c / 2
            elif c < dn_level:
                qty = 200.0 / c
                pos = {'side': 'SHORT', 'entry': c, 'qty': qty, 'sl': c + sl_atr * a}
                balance -= qty * c / 2
        else:
            exit_lo = float(lows[i - exit_n - 1:i - 1].min())
            exit_hi = float(highs[i - exit_n - 1:i - 1].max())
            px = None
            if pos['side'] == 'LONG':
                if l <= pos['sl']:
                    px = pos['sl']
                elif c < exit_lo:
                    px = c
            else:
                if h >= pos['sl']:
                    px = pos['sl']
                elif c > exit_hi:
                    px = c
            if px is not None:
                sign = 1 if pos['side'] == 'LONG' else -1
                gross = (px - pos['entry']) * pos['qty'] * sign
                fees = (pos['entry'] * pos['qty'] + px * pos['qty']) * fee
                pnl = gross - fees
                balance += pos['qty'] * pos['entry'] / 2 + pnl
                trades.append(pnl)
                pos = None
        eq = balance
        if pos:
            sign = 1 if pos['side'] == 'LONG' else -1
            eq += (c - pos['entry']) * pos['qty'] * sign
        equity_curve.append(eq)
    if pos:
        px = closes[-1]
        sign = 1 if pos['side'] == 'LONG' else -1
        gross = (px - pos['entry']) * pos['qty'] * sign
        fees = (pos['entry'] * pos['qty'] + px * pos['qty']) * fee
        balance += pos['qty'] * pos['entry'] / 2 + gross - fees
        trades.append(gross - fees)
    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    dd = float(((eq - peak) / peak).min() * 100) if len(eq) else 0.0
    wins = [t for t in trades if t > 0]
    ret = (balance - initial) / initial * 100
    return dict(trades=len(trades), win_rate=len(wins) / max(1, len(trades)) * 100,
                ret=ret, dd=dd, final=balance)


ex = BinanceFutures(load_config(), base_url=MAINNET_BASE_URL)
print('=' * 100)
print('Donchian 通道突破趋势跟踪 | 6个月 1h 数据 | 单笔200U | 手续费0.05%双边')
print('=' * 100)
for sym in ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'BNBUSDT']:
    kl = ex.get_klines_range(sym, '1h', max_bars=4500)
    print(f'--- {sym} ({len(kl)}根) ---')
    for en, xn, sla in [(55, 20, 2.5), (40, 15, 3.0), (80, 30, 2.0)]:
        r = donchian_backtest(kl, en, xn, sla)
        print(f'  入场{en}根/出场{xn}根/止损{sla}ATR: {r["trades"]:>3}笔 '
              f'胜率{r["win_rate"]:>5.1f}% 收益{r["ret"]:>7.2f}% 最大回撤{r["dd"]:>5.2f}%')
    print()
