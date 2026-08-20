#!/usr/bin/env python3
"""回测工具: 用主网历史K线验证当前策略参数

用法:
    python backtest.py                        # 默认 BTCUSDT 5m 最近3000根
    python backtest.py --symbol ETHUSDT --timeframe 15m --bars 5000
    python backtest.py --days 30              # 最近30天
    python backtest.py --start 2026-01-01 --end 2026-08-01
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from core.backtest import Backtester
from core.config import load_config
from core.exchange import MAINNET_BASE_URL, BinanceFutures


def main():
    ap = argparse.ArgumentParser(description="Binance 合约策略回测")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--bars", type=int, default=3000, help="K线根数上限")
    ap.add_argument("--days", type=int, default=None, help="取最近N天(与bars取交集)")
    ap.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--balance", type=float, default=10000.0, help="初始资金")
    ap.add_argument("--out", default="data/backtest_result.json", help="结果输出文件")
    args = ap.parse_args()

    cfg = load_config()
    ex = BinanceFutures(cfg, base_url=MAINNET_BASE_URL)

    start_ms = end_ms = None
    if args.days:
        start_ms = int((time.time() - args.days * 86400) * 1000)
    if args.start:
        start_ms = int(time.mktime(time.strptime(args.start, "%Y-%m-%d")) * 1000)
    if args.end:
        end_ms = int(time.mktime(time.strptime(args.end, "%Y-%m-%d")) * 1000) + 86399999

    print(f"拉取数据: {args.symbol} {args.timeframe} ...")
    klines = ex.get_klines_range(args.symbol, args.timeframe, start_ms=start_ms,
                                 end_ms=end_ms, max_bars=args.bars)
    if len(klines) < 200:
        print(f"数据不足: 仅 {len(klines)} 根, 需要至少 200 根")
        sys.exit(1)
    print(f"数据就绪: {len(klines)} 根 K 线")
    min_qty, step_size = ex.lot_size(args.symbol)

    print("回测中 (模拟实盘逻辑: 盘中止盈止损/手续费/信号平仓) ...")
    result = Backtester(cfg, args.symbol, args.timeframe, initial_balance=args.balance,
                        min_qty=min_qty, step_size=step_size).run(klines)
    stats = result.stats()

    print("\n" + "=" * 52)
    print(f"回测结果: {args.symbol} {args.timeframe} | {len(klines)} 根K线")
    print("=" * 52)
    rows = [
        ("初始资金", f"{result.initial_balance:.2f} U"),
        ("期末权益", f"{stats['final_equity']:.2f} U"),
        ("总收益率", f"{stats['total_return_pct']:+.2f}%"),
        ("总盈亏", f"{stats['total_pnl']:+.2f} U"),
        ("交易次数", f"{stats['trades']} 笔"),
        ("胜率", f"{stats['win_rate']:.1f}%"),
        ("盈亏比(ProfitFactor)", f"{stats['profit_factor']}"),
        ("年化Sharpe", f"{stats['sharpe']}"),
        ("最大回撤", f"{stats['max_drawdown_pct']:.2f}%"),
        ("手续费合计", f"{stats['fees_total']:.2f} U"),
        ("平均单笔盈亏", f"{stats['avg_trade_pnl']:+.2f} U"),
    ]
    for k, v in rows:
        print(f"  {k:<22}{v}")
    print("=" * 52)

    if result.trades:
        print("\n最近10笔:")
        for t in result.trades[-10:]:
            print(f"  {t['side']:<5} {t['qty']:<10} @{t['entry']:<12} -> {t['exit']:<12} "
                  f"{t['pnl']:+.2f}U ({t['pnl_pct']:+.1f}%) {t['reason']}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol": args.symbol, "timeframe": args.timeframe, "bars": len(klines),
        "stats": stats, "trades": result.trades[-100:],
        "equity_curve": result.equity_curve[:: max(1, len(result.equity_curve) // 500)],
        "params": result.params, "ts": time.time(),
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()
