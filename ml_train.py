#!/usr/bin/env python3
"""离线训练 ML 门禁/选择器模型

用法:
    python ml_train.py            # 拉主网历史K线, 训练, 保存 data/ml_filter.pkl
    python ml_train.py --days 540 # 自定义回看天数

模型是纯 numpy 手写逻辑回归 (零外部依赖), 用 Donchian 历史回放做标签,
walk-forward 防泄漏评估。训练不影响线上行为; 训练后 config 里 ml_filter.enabled=true 即生效。
"""
from __future__ import annotations

import argparse
import json
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()

    sys.path.insert(0, ".")
    from core.config import load_config
    from core.ml_filter import MLFilter

    cfg = load_config()
    mlcfg = cfg.setdefault("ml_filter", {})
    model_file = mlcfg.get("model_file", "data/ml_filter.pkl")
    mlcfg.setdefault("days", args.days)

    print(f"开始训练 ML 门禁/选择器 (回看 {args.days} 天) ...", flush=True)
    model = MLFilter.train(cfg, days=args.days)
    model.save(model_file)

    print("\n===== 训练指标 (walk-forward, 防泄漏) =====")
    print(json.dumps(model.metrics, ensure_ascii=False, indent=2))
    print(f"\n模型已保存: {model_file}")
    print(f"门禁阈值: {model.threshold:.3f}")
    print("线上生效: 将 config.yaml 的 ml_filter.enabled 设为 true (本项目默认已开)。")


if __name__ == "__main__":
    main()
