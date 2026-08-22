"""从交易所 userTrades 重建权威成交流水 (下单/卖出) 与平仓统计

userTrades 是交易所逐笔成交 (每个订单可能有多笔成交 fill), 按 orderId 聚合为一条记录:
- 该订单 realizedPnl 合计 != 0 => 卖出(平仓); 否则为下单(开仓)
"""
from __future__ import annotations


def rebuild_from_user_trades(exchange, symbols, limit: int = 500):
    """拉取交易所逐笔成交, 重建 (orders, trades, strategy_stats)

    - orders: 每条成交一条流水, action=OPEN(下单)/CLOSE(卖出)
    - trades: 平仓明细 (用交易所 realizedPnl, 权威值)
    - strategy_stats: 累计交易/胜率/已实现盈亏/手续费 (交易所口径)
    """
    fills = []
    for sym in symbols:
        try:
            data = exchange.get_user_trades(sym, limit)
        except Exception:
            continue
        for f in data or []:
            try:
                fills.append({
                    "ts": int(f["time"]) / 1000.0,
                    "symbol": sym,
                    "order_id": str(f.get("orderId", "")),
                    "side": f["side"],
                    "price": float(f["price"]),
                    "qty": float(f["qty"]),
                    "fees": float(f.get("commission", 0.0)),
                    "pnl": float(f.get("realizedPnl", 0.0)),
                })
            except (KeyError, TypeError, ValueError):
                continue

    # 按 (symbol, orderId) 聚合: 一个市价单多次成交合并为一条
    orders: dict[tuple, dict] = {}
    for f in fills:
        key = (f["symbol"], f["order_id"])
        g = orders.get(key)
        if g is None:
            g = {
                "ts": f["ts"],
                "symbol": f["symbol"],
                "order_id": f["order_id"],
                "side": f["side"],
                "qty": 0.0,
                "price_w": 0.0,
                "fees": 0.0,
                "pnl": 0.0,
            }
            orders[key] = g
        g["ts"] = min(g["ts"], f["ts"])
        g["qty"] += f["qty"]
        g["price_w"] += f["price"] * f["qty"]
        g["fees"] += f["fees"]
        g["pnl"] += f["pnl"]

    result: list[dict] = []
    trades: list[dict] = []
    for key, g in orders.items():
        avg_price = g["price_w"] / g["qty"] if g["qty"] else 0.0
        avg_price = round(avg_price, 8)
        g["qty"] = round(g["qty"], 8)
        g["fees"] = round(g["fees"], 8)
        is_close = abs(g["pnl"]) > 1e-9
        if is_close:
            # 卖出(平仓): BUY平空 / SELL平多
            side = "SHORT" if g["side"] == "BUY" else "LONG"
            result.append({
                "ts": g["ts"], "symbol": g["symbol"], "action": "CLOSE", "side": side,
                "qty": g["qty"], "price": avg_price, "fees": g["fees"], "pnl": g["pnl"],
                "pnl_pct": None, "reason": None, "strategy": None,
            })
            trades.append({
                "ts": g["ts"], "symbol": g["symbol"], "side": side, "qty": g["qty"],
                "entry": None, "exit": avg_price, "leverage": 1, "pnl": g["pnl"],
                "pnl_pct": None, "fees": g["fees"], "reason": None, "strategy": None,
            })
        else:
            # 下单(开仓): BUY开多 / SELL开空
            side = "LONG" if g["side"] == "BUY" else "SHORT"
            result.append({
                "ts": g["ts"], "symbol": g["symbol"], "action": "OPEN", "side": side,
                "qty": g["qty"], "price": avg_price, "fees": g["fees"], "pnl": None,
                "pnl_pct": None, "reason": None, "strategy": None,
            })

    result.sort(key=lambda o: o["ts"])
    trades.sort(key=lambda t: t["ts"])
    wins = sum(1 for t in trades if t["pnl"] > 0)
    stats = {
        "total_trades": len(trades),
        "wins": wins,
        "realized_pnl": round(sum(t["pnl"] for t in trades), 4),
        "fees_paid": round(sum(o["fees"] for o in result), 4),
    }
    return result, trades, stats
