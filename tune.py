#!/usr/bin/env python3
"""AI 自动调参工具: 用回测评估, 随机搜索+爬山优化参数

用法:
    python tune.py                          # 默认 BTCUSDT 5m 2000根 20次尝试
    python tune.py --symbol ETHUSDT --trials 30 --bars 3000
    python tune.py --dry-run                # 只评估不写 tuned_params.json
"""
from __future__ import annotations

import argparse
import json
import sys

from core.config import load_config
from core.tuner import run_tuning, save_tuned_params


def main():
    ap = argparse.ArgumentParser(description="AI 参数自动优化")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--bars", type=int, default=2000)
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="只评估, 不写入配置")
    args = ap.parse_args()

    cfg = load_config()
    print(f"AI 参数优化: {args.symbol} {args.timeframe} | {args.bars}根K线 | {args.trials}次尝试")
    print("(可能需要几分钟, 请耐心等待)\n")

    result = run_tuning(
        cfg, symbol=args.symbol, timeframe=args.timeframe,
        bars=args.bars, trials=args.trials, seed=args.seed,
    )
    if result.get("error"):
        print(f"失败: {result['error']}")
        sys.exit(1)

    print("=" * 52)
    print(f"基线绩效: {result['base_stats']['trades']}笔 | "
          f"收益{result['base_stats']['total_return_pct']}% | "
          f"Sharpe {result['base_stats']['sharpe']} | "
          f"回撤{result['base_stats']['max_drawdown_pct']}%")
    print(f"基线评分: {result['base_score']}  ->  最优评分: {result['score']}")
    print(f"改进: {'✅ 是' if result['improved'] else '❌ 否 (当前参数已较优)'}")
    print("=" * 52)

    if result["improved"]:
        print("\n最优参数:")
        for k, v in sorted(result["params"].items()):
            print(f"  {k:<38}{v}")
        if not args.dry_run:
            path = save_tuned_params(result)
            print(f"\n✅ 已写入 {path}, 重启系统或等下次定时调参自动生效")
    else:
        print("\n未发现显著更优的参数组合, 保持当前配置不变")


if __name__ == "__main__":
    main()
