"""Binance U本位合约 客户端 (REST)

- live/paper: 默认连测试网 testnet.binancefuture.com
- backtest: 可传入 base_url="https://fapi.binance.com" 使用主网历史数据
"""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from typing import Any

import requests

from .logger import get_logger

DEFAULT_BASE_URL = "https://testnet.binancefuture.com"
MAINNET_BASE_URL = "https://fapi.binance.com"
RECV_WINDOW = 10000


class ExchangeError(Exception):
    pass


class BinanceFutures:
    def __init__(self, cfg: dict, base_url: str | None = None):
        self.cfg = cfg
        self.log = get_logger("exchange")
        self.base_url = base_url or DEFAULT_BASE_URL
        self.mode = cfg["mode"]
        self.api_key = cfg["api"]["key"]
        self.api_secret = cfg["api"]["secret"]
        if self.mode == "live" and (not self.api_key or not self.api_secret):
            raise ExchangeError("live 模式需要配置 BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET")
        self.session = requests.Session()
        self._info_cache: dict[str, Any] | None = None

    # ---------------- 基础请求 ----------------
    def _public(self, method: str, path: str, **params) -> Any:
        url = self.base_url + path
        resp = self.session.request(method, url, params=params, timeout=20)
        return self._handle(resp)

    def _signed(self, method: str, path: str, **params) -> Any:
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = RECV_WINDOW
        query = urllib.parse.urlencode(params)
        sig = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"{self.base_url}{path}?{query}&signature={sig}"
        headers = {"X-MBX-APIKEY": self.api_key}
        resp = self.session.request(method, url, headers=headers, timeout=15)
        return self._handle(resp)

    @staticmethod
    def _handle(resp: requests.Response) -> Any:
        try:
            data = resp.json()
        except ValueError:
            raise ExchangeError(f"非JSON响应 {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ExchangeError(f"API错误 {resp.status_code}: {data}")
        return data

    # ---------------- 公开行情 ----------------
    def ping(self) -> bool:
        try:
            self._public("GET", "/fapi/v1/ping")
            return True
        except Exception as e:
            self.log.warning("测试网连接失败: %s", e)
            return False

    def get_klines(self, symbol: str, interval: str, limit: int = 300) -> list[dict]:
        raw = self._public("GET", "/fapi/v1/klines", symbol=symbol, interval=interval, limit=limit)
        return self._parse_klines(raw)

    def get_klines_range(self, symbol: str, interval: str, start_ms: int | None = None,
                         end_ms: int | None = None, max_bars: int = 5000) -> list[dict]:
        """按时间范围拉取历史K线 (自动分页, 用于回测)

        - start_ms 给定: 从该时间向前翻页 (可指定 end_ms 截止)
        - start_ms 未给: 取最新数据, 第一页即最新, 再向前翻页补齐
        """
        page = 1500

        if start_ms is None:
            raw = self._public("GET", "/fapi/v1/klines", symbol=symbol,
                               interval=interval, limit=min(page, max_bars))
            if not raw:
                return []
            out = self._parse_klines(raw)
            cur_end = int(raw[0][0]) - 1
            while len(out) < max_bars:
                params = dict(symbol=symbol, interval=interval, endTime=cur_end,
                              limit=min(page, max_bars - len(out)))
                batch_raw = self._public("GET", "/fapi/v1/klines", **params)
                if not batch_raw:
                    break
                batch = self._parse_klines(batch_raw)
                out = batch + out  # 向前翻页, 前插
                if len(batch) < page:
                    break
                cur_end = int(batch_raw[0][0]) - 1
            return out

        # 给定 start_ms: 向前翻页
        out: list[dict] = []
        cur_start = start_ms
        while len(out) < max_bars:
            params: dict = dict(symbol=symbol, interval=interval,
                                limit=min(page, max_bars - len(out)))
            if cur_start:
                params["startTime"] = cur_start
            if end_ms:
                params["endTime"] = end_ms
            raw = self._public("GET", "/fapi/v1/klines", **params)
            if not raw:
                break
            batch = self._parse_klines(raw)
            out.extend(batch)
            if len(batch) < page:
                break
            last_open = int(raw[-1][0])
            if end_ms and last_open >= end_ms:
                break
            cur_start = last_open + 1
        return out

    @staticmethod
    def _parse_klines(raw: list) -> list[dict]:
        out = []
        for k in raw:
            out.append(
                {
                    "open_time": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": int(k[6]),
                }
            )
        return out

    def get_all_prices(self) -> dict[str, float]:
        """全部交易对最新价 {symbol: price}"""
        data = self._public("GET", "/fapi/v1/ticker/price")
        return {d["symbol"]: float(d["price"]) for d in data}

    def get_all_24hr(self) -> dict[str, dict]:
        """全部交易对24小时行情, 取 priceChangePercent"""
        data = self._public("GET", "/fapi/v1/ticker/24hr")
        out = {}
        for d in data:
            try:
                out[d["symbol"]] = {
                    "price": float(d["lastPrice"]),
                    "change_pct": float(d["priceChangePercent"]),
                    "volume": float(d["quoteVolume"]),
                }
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def get_mark_prices(self) -> dict[str, float]:
        """全部交易对标记价格 {symbol: mark}"""
        data = self._public("GET", "/fapi/v1/premiumIndex")
        return {d["symbol"]: float(d["markPrice"]) for d in data}

    def get_exchange_info(self) -> dict[str, Any]:
        if self._info_cache is None:
            self._info_cache = self._public("GET", "/fapi/v1/exchangeInfo")
        return self._info_cache

    def lot_size(self, symbol: str) -> tuple[float, float]:
        """返回 (minQty, stepSize)"""
        info = self.get_exchange_info()
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        return float(f["minQty"]), float(f["stepSize"])
        return 0.0, 0.000001

    @staticmethod
    def round_qty(qty: float, step_size: float) -> float:
        """按 stepSize 向下取整"""
        if step_size <= 0:
            return qty
        s = f"{step_size:.10f}".rstrip("0")
        decimals = len(s.split(".")[1]) if "." in s else 0
        factor = 10 ** decimals
        return int(qty * factor) / factor

    # ---------------- 签名账户/交易接口 ----------------
    # 注意: 测试网只支持 v2 账户接口 (v1 返回 404)
    def get_account(self) -> dict[str, Any]:
        return self._signed("GET", "/fapi/v2/account")

    def get_position_risk(self, symbol: str | None = None) -> list[dict]:
        params = {} if symbol is None else {"symbol": symbol}
        return self._signed("GET", "/fapi/v2/positionRisk", **params)

    def get_user_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        """用户成交明细 (逐笔成交, 交易所权威记录)"""
        return self._signed("GET", "/fapi/v1/userTrades", symbol=symbol, limit=int(limit))

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._signed("POST", "/fapi/v1/leverage", symbol=symbol, leverage=int(leverage))

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        return self._signed("POST", "/fapi/v1/marginType", symbol=symbol, marginType=margin_type)

    def market_order(self, symbol: str, side: str, quantity: float, reduce_only: bool = False) -> dict:
        """side: BUY / SELL; reduce_only=True 时只减仓不反向开仓"""
        params = dict(symbol=symbol, side=side, type="MARKET", quantity=quantity)
        if reduce_only:
            params["reduceOnly"] = "true"
        return self._signed("POST", "/fapi/v1/order", **params)

    def place_stop_market(self, symbol: str, side: str, stop_price: float,
                          quantity: float | None = None, reduce_only: bool = True) -> dict:
        """向交易所挂 STOP_MARKET 止损单 (Algo Order API, 交易所侧自动执行)

        Binance 2025-12 起要求条件单走 /fapi/v1/algoOrder:
        - algoType=CONDITIONAL, type=STOP_MARKET, triggerPrice=止损价
        - closePosition=true 时按整个持仓平仓 (不可同时传 quantity/reduceOnly)
        side: 与持仓相反 (多单挂 SELL, 空单挂 BUY)
        """
        params = dict(
            algoType="CONDITIONAL",
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            triggerPrice=stop_price,
            workingType="MARK_PRICE",
            closePosition="true",
        )
        return self._signed("POST", "/fapi/v1/algoOrder", **params)

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """查询未成交挂单 (含算法止损单)"""
        params = {} if symbol is None else {"symbol": symbol}
        algo = self._signed("GET", "/fapi/v1/openAlgoOrders", **params)
        try:
            normal = self._signed("GET", "/fapi/v1/openOrders", **params)
        except Exception:
            normal = []
        return list(algo or []) + list(normal or [])

    def cancel_all_orders(self, symbol: str | None = None) -> None:
        """取消未成交挂单 (含算法止损单; 平仓前必须调用, 否则触发反向开仓)"""
        try:
            self._signed("DELETE", "/fapi/v1/allOpenOrders",
                         **({} if symbol is None else {"symbol": symbol}))
        except Exception:
            pass
        try:
            algos = self._signed("GET", "/fapi/v1/openAlgoOrders",
                                 **({} if symbol is None else {"symbol": symbol}))
            for a in algos or []:
                aid = a.get("algoId")
                if aid:
                    try:
                        self._signed("DELETE", "/fapi/v1/algoOrder", algoId=aid)
                    except Exception as e:
                        self.log.warning("取消算法单失败 algoId=%s: %s", aid, e)
        except Exception as e:
            self.log.warning("查询算法单失败: %s", e)

    def close_all(self) -> None:
        """平掉所有持仓 (live 模式熔断时用)"""
        risks = self.get_position_risk()
        for r in risks:
            amt = float(r.get("positionAmt", 0))
            symbol = r.get("symbol", "")
            if abs(amt) < 1e-8:
                continue
            side = "SELL" if amt > 0 else "BUY"
            try:
                self.market_order(symbol, side, abs(amt))
                self.log.info("[live] 熔断平仓 %s %s %s", symbol, side, abs(amt))
            except Exception as e:
                self.log.error("[live] 熔断平仓失败 %s: %s", symbol, e)
