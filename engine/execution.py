"""执行层: PaperExecution(模拟撮合) / LiveExecution(真实测试网下单)"""
from __future__ import annotations

from core.logger import get_logger


class BaseExecution:
    def __init__(self, cfg, state, exchange):
        self.cfg = cfg
        self.state = state
        self.exchange = exchange
        self.log = get_logger("execution")

    async def open_position(self, symbol, side, qty, price, leverage, notional,
                            tp, sl, atr, strategy) -> bool:
        raise NotImplementedError

    async def close_position(self, symbol, exit_price, reason) -> dict | None:
        raise NotImplementedError


class PaperExecution(BaseExecution):
    """模拟下单: 按当前价格直接成交, 扣除双边手续费"""

    async def open_position(self, symbol, side, qty, price, leverage, notional,
                            tp, sl, atr, strategy) -> bool:
        self.state.open_position(
            symbol=symbol, side=side, qty=qty, entry=price, leverage=leverage,
            notional=notional, upnl=0.0, tp=tp, sl=sl, atr=atr, strategy=strategy,
        )
        self.log.info("[paper] 开仓 %s %s %s@%.2f 杠杆%d 名义%.0fU TP=%.2f SL=%.2f (策略:%s)",
                      symbol, side, qty, price, leverage, notional, tp, sl, strategy)
        self.state.save()
        return True

    async def close_position(self, symbol, exit_price, reason) -> dict | None:
        trade = self.state.close_position(symbol, exit_price, reason)
        if trade:
            self.log.info("[paper] 平仓 %s %s %.4f@%.2f 盈亏 %.2fU (%.2f%%) 原因:%s",
                          symbol, trade["side"], trade["qty"], exit_price,
                          trade["pnl"], trade["pnl_pct"], reason)
            self.state.save()
        return trade


class LiveExecution(BaseExecution):
    """真实测试网下单: 市价单 + 逐仓模式 + 动态杠杆"""

    def __init__(self, cfg, state, exchange):
        super().__init__(cfg, state, exchange)
        self._margin_configured = set()

    def _setup_margin(self, symbol: str, leverage: int) -> None:
        if symbol not in self._margin_configured:
            try:
                self.exchange.set_margin_type(symbol, "ISOLATED")
            except Exception as e:
                self.log.warning("[%s] 设置逐仓失败(可能已设置): %s", symbol, e)
            self._margin_configured.add(symbol)
        try:
            self.exchange.set_leverage(symbol, leverage)
        except Exception as e:
            self.log.warning("[%s] 设置杠杆失败: %s", symbol, e)

    async def open_position(self, symbol, side, qty, price, leverage, notional,
                            tp, sl, atr, strategy) -> bool:
        try:
            self._setup_margin(symbol, leverage)
            order_side = "BUY" if side == "LONG" else "SELL"
            order = await self._to_thread(self.exchange.market_order, symbol, order_side, qty)
            fill_price = float(order.get("avgPrice") or price)
            fill_qty = float(order.get("executedQty") or qty)
            self.state.open_position(
                symbol=symbol, side=side, qty=fill_qty, entry=fill_price,
                leverage=leverage, notional=fill_qty * fill_price, upnl=0.0,
                tp=tp, sl=sl, atr=atr, strategy=strategy,
            )
            self.log.info("[live] 开仓 %s %s %.4f@%.2f 杠杆%d (策略:%s)",
                          symbol, order_side, fill_qty, fill_price, leverage, strategy)
            # 把止损下到交易所: 交易所侧自动执行, 本地断网/行情断流也能止损
            if sl and sl > 0:
                stop_side = "SELL" if side == "LONG" else "BUY"
                try:
                    await self._to_thread(
                        self.exchange.place_stop_market, symbol, stop_side, sl, fill_qty
                    )
                    self.log.info("[live] 交易所止损单已挂 %s %s SL=%.2f qty=%.4f",
                                  symbol, stop_side, sl, fill_qty)
                except Exception as e:
                    self.log.error("[live] 挂止损单失败 %s: %s", symbol, e)
            self.state.save()
            return True
        except Exception as e:
            self.log.error("[live] 开仓失败 %s %s: %s", symbol, side, e)
            return False

    async def close_position(self, symbol, exit_price, reason) -> dict | None:
        pos = self.state.data["positions"].get(symbol)
        if not pos:
            return None
        try:
            # 平仓前先撤掉交易所侧的止损单, 避免触发反向开仓
            try:
                await self._to_thread(self.exchange.cancel_all_orders, symbol)
            except Exception as e:
                self.log.warning("[live] 撤单失败 %s: %s", symbol, e)
            order_side = "SELL" if pos["side"] == "LONG" else "BUY"
            order = await self._to_thread(
                self.exchange.market_order, symbol, order_side, pos["qty"], reduce_only=True
            )
            fill_price = float(order.get("avgPrice") or exit_price)
            trade = self.state.close_position(symbol, fill_price, reason)
            if trade:
                self.log.info("[live] 平仓 %s %s %.4f@%.2f 盈亏 %.2fU 原因:%s",
                              symbol, pos["side"], pos["qty"], fill_price, trade["pnl"], reason)
                self.state.save()
            return trade
        except Exception as e:
            self.log.error("[live] 平仓失败 %s: %s", symbol, e)
            return None

    @staticmethod
    async def _to_thread(fn, *args):
        import asyncio
        return await asyncio.to_thread(fn, *args)
