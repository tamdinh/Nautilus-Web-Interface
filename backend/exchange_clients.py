"""
Signed REST clients for Binance Spot and Bybit (v5).

NautilusTrader 2.x no longer exposes standalone Python HTTP clients for these exchanges (its
adapters are built around TradingNode), so LiveTradingManager talks to the REST APIs directly.

Design notes (verified against the official docs, 2026-10-29):
* Every order carries OUR client order id (Binance `newClientOrderId`, Bybit `orderLinkId`), so an
  order whose submit outcome is unknown can always be found again with `get_order`.
* Outcome classification matters more than the happy path. `ExchangeHTTPError.outcome` is
  "rejected" only when the venue certainly did not create the order; 5xx, timeouts, rate-limit
  responses (429/418, Bybit 10006) and malformed replies are "unknown" — the caller must reconcile
  by client order id, never assume failure and never blindly retry.
* `ExchangeTransportError.maybe_sent` is False only when the failure happened before any request
  bytes could have been written (connect errors / pool timeouts).
* Clock: signed requests use server time (offset from /time); a timestamp rejection (-1021 /
  10002) is a definite rejection, so one resync + retry is safe.
* Rate limiting: after 429/418 (Binance) or 10006/403 (Bybit) further requests are refused locally
  until Retry-After has elapsed.

Base URLs can be overridden with BINANCE_BASE_URL / BYBIT_BASE_URL (testnet:
https://testnet.binance.vision, https://api-testnet.bybit.com — testnet needs its own API keys).
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

# Binance codes meaning the request was refused and no order exists. Everything else is treated as
# UNKNOWN (e.g. -1000 "unknown error", -1001 "disconnected", odd 4xx): the venue may have processed it.
_BINANCE_DEFINITE = {-1002, -1013, -1021, -1022, -2010, -2011, -2013, -2014, -2015, -2018, -2019, -2020,
                     -2021, -2022, -2026}
_BINANCE_PARAM_RANGE = (-1199, -1100)   # malformed/illegal parameter errors (-11xx)
_BINANCE_UNKNOWN = {-1000, -1001, -1006, -1007, -1008, -1003, -1015, -1016}
_BINANCE_TIME_CODE = -1021
_BINANCE_NOT_FOUND = {-2013}
# Bybit: only these are definite refusals; any other non-zero retCode is UNKNOWN until the order is looked up
_BYBIT_DEFINITE_CODES = {10001, 10002, 10003, 10004, 10005, 10007, 10010, 33004}
_BYBIT_DEFINITE_RANGES = ((110000, 119999), (170000, 179999))   # UTA / spot order-level rejections
_BYBIT_UNKNOWN_CODES = {10006, 10016, 10000, 10018}
_BYBIT_TIME_CODE = 10002
_BYBIT_NOT_FOUND = {170213, 110001, 170142, 170143}


class ExchangeError(Exception):
    """Base class for exchange client errors."""


class ExchangeHTTPError(ExchangeError):
    """Exchange returned an error. `status` is the HTTP status, `code` the exchange error code."""

    def __init__(self, status: int, message: str, code: Optional[int] = None,
                 venue: str = "", retry_after: Optional[float] = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.code = code
        self.venue = venue
        self.retry_after = retry_after

    @property
    def is_auth(self) -> bool:
        return (self.status in (401, 403) and not self.is_rate_limit) or self.code in (
            -1022, -2014, -2015, 10003, 10004, 10005, 10007, 10010, 33004)

    @property
    def is_rate_limit(self) -> bool:
        return self.status in (418, 429) or self.code in (-1003, -1015, 10006) or (
            self.venue == "BYBIT" and self.status == 403 and self.code is None)

    @property
    def is_not_found(self) -> bool:
        return self.code in _BINANCE_NOT_FOUND or self.code in _BYBIT_NOT_FOUND

    @property
    def is_timestamp(self) -> bool:
        return self.code in (_BINANCE_TIME_CODE, _BYBIT_TIME_CODE)

    @property
    def outcome(self) -> str:
        """'rejected' = the venue certainly did not create/accept the order; 'unknown' otherwise."""
        if self.is_rate_limit or self.status >= 500 or self.status == 0:
            return "unknown"
        if self.venue == "BINANCE":
            code = self.code
            if code in _BINANCE_UNKNOWN:
                return "unknown"
            if code is not None and (code in _BINANCE_DEFINITE
                                     or _BINANCE_PARAM_RANGE[0] <= code <= _BINANCE_PARAM_RANGE[1]):
                return "rejected"
            return "rejected" if (self.status in (401, 403) and self.is_auth) else "unknown"
        if self.venue == "BYBIT":
            code = self.code
            if code is None or code in _BYBIT_UNKNOWN_CODES:
                return "unknown"
            if code in _BYBIT_DEFINITE_CODES or any(lo <= code <= hi for lo, hi in _BYBIT_DEFINITE_RANGES):
                return "rejected"
            return "unknown"
        return "unknown"


class ExchangeTransportError(ExchangeError):
    """Network-level failure. `maybe_sent` is False only if no request bytes can have been written."""

    def __init__(self, message: str, maybe_sent: bool) -> None:
        super().__init__(message)
        self.maybe_sent = maybe_sent

    @property
    def outcome(self) -> str:
        return "unknown" if self.maybe_sent else "not_sent"


class ExchangeRateLimited(ExchangeError):
    """Raised locally (nothing sent) while a Retry-After window from the venue is still active."""

    def __init__(self, retry_in: float) -> None:
        super().__init__(f"rate limited by venue; retry in {retry_in:.0f}s")
        self.retry_in = retry_in
        self.outcome = "not_sent"


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _fmt(d: Optional[Decimal]) -> Optional[str]:
    """Plain decimal string (never scientific notation)."""
    return None if d is None else format(d, "f")


@dataclass
class VenueOrder:
    """Normalised view of an order as reported by a venue."""
    venue: str
    venue_order_id: str
    client_order_id: str
    raw_status: str
    executed_qty: Decimal = Decimal(0)
    avg_price: Optional[Decimal] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SymbolRules:
    """Trading filters for one symbol."""
    venue: str
    symbol: str
    tick_size: Optional[Decimal] = None
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None
    step_size: Optional[Decimal] = None
    min_qty: Optional[Decimal] = None
    max_qty: Optional[Decimal] = None
    market_step_size: Optional[Decimal] = None
    market_max_qty: Optional[Decimal] = None
    min_notional: Optional[Decimal] = None
    max_notional: Optional[Decimal] = None
    notional_applies_to_market: bool = True
    trading: bool = True


class _BaseClient:
    venue = ""
    _time_path = ""

    def __init__(self, api_key: str, api_secret: str, base_url: str, timeout: float, recv_window: int) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._recv_window = recv_window
        self._offset_ms = 0
        self._blocked_until = 0.0
        self._synced = False

    # ── time & rate limiting ───────────────────────────────────────────────────
    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._offset_ms

    def _check_gate(self) -> None:
        remaining = self._blocked_until - time.monotonic()
        if remaining > 0:
            raise ExchangeRateLimited(remaining)

    def _note_rate_limit(self, retry_after: Optional[float]) -> None:
        self._blocked_until = max(self._blocked_until, time.monotonic() + (retry_after or 10.0))

    async def _http(self, method: str, url: str, headers: Dict[str, str], content: Optional[str] = None) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                return await client.request(method, url, headers=headers, content=content)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol,
                httpx.LocalProtocolError) as exc:
            raise ExchangeTransportError(f"{self.venue} unreachable: {exc}", maybe_sent=False) from exc
        except httpx.HTTPError as exc:
            raise ExchangeTransportError(f"{self.venue} request failed after send: {exc!r}", maybe_sent=True) from exc

    @staticmethod
    def _retry_after(resp: httpx.Response) -> Optional[float]:
        try:
            return float(resp.headers.get("Retry-After", ""))
        except ValueError:
            return None

    async def sync_time(self) -> int:
        """Fetch venue time and store the offset; returns the offset in ms."""
        raise NotImplementedError


# Binance Spot environments. "demo" is Binance Demo Mode (fake funds, keys from demo.binance.com);
# "live" is the production exchange (real money).
BINANCE_ENVIRONMENTS = {
    "demo": "https://demo-api.binance.com",
    "live": "https://api.binance.com",
}
DEFAULT_BINANCE_ENVIRONMENT = "demo"


class BinanceRestClient(_BaseClient):
    """Binance Spot signed REST client (HMAC-SHA256)."""

    venue = "BINANCE"

    def __init__(self, api_key: str, api_secret: str, base_url: Optional[str] = None,
                 timeout: float = 10.0, recv_window: int = 5000,
                 environment: str = DEFAULT_BINANCE_ENVIRONMENT) -> None:
        if environment not in BINANCE_ENVIRONMENTS:
            raise ValueError(f"unknown Binance environment: {environment!r}")
        self.environment = environment
        # an explicit base_url / BINANCE_BASE_URL (tests, proxies) wins over the environment's URL
        super().__init__(api_key, api_secret,
                         base_url or os.getenv("BINANCE_BASE_URL") or BINANCE_ENVIRONMENTS[environment],
                         timeout, recv_window)

    async def sync_time(self) -> int:
        t0 = int(time.time() * 1000)
        body = await self._request("GET", "/api/v3/time", signed=False, _no_sync=True)
        t1 = int(time.time() * 1000)
        self._offset_ms = int(body["serverTime"]) - (t0 + t1) // 2
        self._synced = True
        return self._offset_ms

    async def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                       signed: bool = True, _retry: bool = True, _no_sync: bool = False) -> Any:
        self._check_gate()
        if signed and not self._synced and not _no_sync:
            try:
                await self.sync_time()
            except ExchangeError:
                pass  # fall back to local clock; a real skew will surface as -1021 below
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            params["timestamp"] = self._now_ms()
            params["recvWindow"] = self._recv_window
            query = urlencode(params)
            query += "&signature=" + _sign(self._api_secret, query)
        else:
            query = urlencode(params)
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        resp = await self._http(method, url, {"X-MBX-APIKEY": self._api_key})
        try:
            body = resp.json()
        except ValueError:
            raise ExchangeHTTPError(resp.status_code if resp.status_code >= 400 else 502,
                                    "invalid JSON response from Binance", venue=self.venue)
        if resp.status_code >= 400:
            err = ExchangeHTTPError(resp.status_code,
                                    str(body.get("msg", body)) if isinstance(body, dict) else str(body),
                                    body.get("code") if isinstance(body, dict) else None,
                                    venue=self.venue, retry_after=self._retry_after(resp))
            if err.is_rate_limit:
                self._note_rate_limit(err.retry_after)
            if err.is_timestamp and _retry and signed:
                await self.sync_time()  # -1021 is a definite rejection: safe to retry once
                return await self._request(method, path, params, signed, _retry=False)
            raise err
        return body

    # ── account ───────────────────────────────────────────────────────────────
    async def account_info(self) -> Dict[str, Any]:
        return await self._request("GET", "/api/v3/account")

    async def ticker_price(self, symbol: str) -> Decimal:
        body = await self._request("GET", "/api/v3/ticker/price", {"symbol": symbol}, signed=False)
        return Decimal(str(body["price"]))

    async def all_ticker_prices(self) -> Dict[str, Decimal]:
        body = await self._request("GET", "/api/v3/ticker/price", signed=False)
        return {t["symbol"]: Decimal(str(t["price"])) for t in body}

    # ── orders ────────────────────────────────────────────────────────────────
    async def new_order(self, symbol: str, side: str, order_type: str, quantity: str,
                        price: Optional[str] = None, time_in_force: Optional[str] = None,
                        client_order_id: Optional[str] = None) -> Dict[str, Any]:
        return await self._request("POST", "/api/v3/order", {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
            "price": price,
            "timeInForce": time_in_force,
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        })

    async def get_order(self, symbol: str, client_order_id: Optional[str] = None,
                        order_id: Optional[str] = None) -> Optional[VenueOrder]:
        """Return the order, or None if the venue says it does not exist (-2013)."""
        try:
            body = await self._request("GET", "/api/v3/order", {
                "symbol": symbol, "origClientOrderId": client_order_id,
                "orderId": int(order_id) if order_id and str(order_id).isdigit() else None,
            })
        except ExchangeHTTPError as exc:
            if exc.is_not_found:
                return None
            raise
        return self.parse_order(body)

    def parse_order(self, body: Dict[str, Any]) -> VenueOrder:
        executed = Decimal(str(body.get("executedQty", "0") or "0"))
        quote = Decimal(str(body.get("cummulativeQuoteQty", "0") or "0"))
        avg = (quote / executed) if executed > 0 and quote > 0 else None
        return VenueOrder(self.venue, str(body.get("orderId", "")), str(body.get("clientOrderId", "")),
                          str(body.get("status", "")), executed, avg, body)

    async def cancel_order(self, symbol: str, order_id: Optional[str] = None,
                           client_order_id: Optional[str] = None) -> Dict[str, Any]:
        return await self._request("DELETE", "/api/v3/order", {
            "symbol": symbol,
            "orderId": int(order_id) if order_id and str(order_id).isdigit() else None,
            "origClientOrderId": client_order_id,
        })

    async def open_orders(self, symbol: str) -> List[VenueOrder]:
        body = await self._request("GET", "/api/v3/openOrders", {"symbol": symbol})
        return [self.parse_order(o) for o in body]

    async def symbol_rules(self, symbol: str) -> SymbolRules:
        body = await self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol}, signed=False)
        syms = body.get("symbols") or []
        if not syms:
            raise ExchangeHTTPError(400, f"unknown symbol {symbol}", -1121, venue=self.venue)
        s = syms[0]
        rules = SymbolRules(self.venue, symbol, trading=(s.get("status") == "TRADING"))
        D = lambda v: Decimal(str(v)) if v not in (None, "") else None  # noqa: E731
        for f in s.get("filters", []):
            t = f.get("filterType")
            if t == "PRICE_FILTER":
                rules.tick_size = D(f.get("tickSize")) or None
                rules.min_price, rules.max_price = D(f.get("minPrice")), D(f.get("maxPrice"))
            elif t == "LOT_SIZE":
                rules.step_size, rules.min_qty, rules.max_qty = D(f.get("stepSize")), D(f.get("minQty")), D(f.get("maxQty"))
            elif t == "MARKET_LOT_SIZE":
                rules.market_step_size, rules.market_max_qty = D(f.get("stepSize")), D(f.get("maxQty"))
            elif t == "NOTIONAL":
                rules.min_notional, rules.max_notional = D(f.get("minNotional")), D(f.get("maxNotional"))
                rules.notional_applies_to_market = bool(f.get("applyMinToMarket", True))
            elif t == "MIN_NOTIONAL" and rules.min_notional is None:
                rules.min_notional = D(f.get("minNotional"))
                rules.notional_applies_to_market = bool(f.get("applyToMarket", True))
        return rules


class BybitRestClient(_BaseClient):
    """Bybit v5 signed REST client (HMAC-SHA256)."""

    venue = "BYBIT"

    def __init__(self, api_key: str, api_secret: str, base_url: Optional[str] = None,
                 timeout: float = 10.0, recv_window: int = 5000) -> None:
        super().__init__(api_key, api_secret,
                         base_url or os.getenv("BYBIT_BASE_URL", "https://api.bybit.com"),
                         timeout, recv_window)

    async def sync_time(self) -> int:
        t0 = int(time.time() * 1000)
        result = await self._request("GET", "/v5/market/time", signed=False, _no_sync=True)
        t1 = int(time.time() * 1000)
        if result.get("timeNano"):
            server_ms = int(result["timeNano"]) // 1_000_000
        else:
            server_ms = int(result["timeSecond"]) * 1000
        self._offset_ms = server_ms - (t0 + t1) // 2
        self._synced = True
        return self._offset_ms

    async def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                       signed: bool = True, _retry: bool = True, _no_sync: bool = False) -> Dict[str, Any]:
        self._check_gate()
        if signed and not self._synced and not _no_sync:
            try:
                await self.sync_time()
            except ExchangeError:
                pass
        params = {k: v for k, v in (params or {}).items() if v is not None}
        recv = str(self._recv_window)
        if method == "GET":
            payload = urlencode(params)
            url = f"{self._base_url}{path}" + (f"?{payload}" if payload else "")
            content = None
        else:
            payload = json.dumps(params, separators=(",", ":"))
            url = f"{self._base_url}{path}"
            content = payload
        headers = {"Content-Type": "application/json"}
        if signed:
            ts = str(self._now_ms())
            headers.update({
                "X-BAPI-API-KEY": self._api_key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": recv,
                "X-BAPI-SIGN": _sign(self._api_secret, ts + self._api_key + recv + payload),
            })
        resp = await self._http(method, url, headers, content)
        try:
            body = resp.json()
        except ValueError:
            raise ExchangeHTTPError(resp.status_code if resp.status_code >= 400 else 502,
                                    "invalid JSON response from Bybit", venue=self.venue)
        if resp.status_code >= 400:
            err = ExchangeHTTPError(resp.status_code, str(body), venue=self.venue,
                                    retry_after=self._retry_after(resp) or (600.0 if resp.status_code == 403 else None))
            if err.is_rate_limit:
                self._note_rate_limit(err.retry_after)
            raise err
        # Bybit reports API errors with HTTP 200 and a non-zero retCode
        ret_code = body.get("retCode", 0) if isinstance(body, dict) else 0
        if ret_code != 0:
            err = ExchangeHTTPError(resp.status_code, str(body.get("retMsg", body)), ret_code, venue=self.venue)
            if err.is_rate_limit:
                self._note_rate_limit(err.retry_after)
            if err.is_timestamp and _retry and signed:
                await self.sync_time()
                return await self._request(method, path, params, signed, _retry=False)
            raise err
        return body.get("result", {})

    # ── account ───────────────────────────────────────────────────────────────
    async def account_info(self) -> Dict[str, Any]:
        return await self._request("GET", "/v5/account/info")

    async def wallet_balance(self, account_type: str = "UNIFIED") -> Dict[str, Any]:
        return await self._request("GET", "/v5/account/wallet-balance", {"accountType": account_type})

    async def ticker_price(self, symbol: str, category: str = "spot") -> Decimal:
        result = await self._request("GET", "/v5/market/tickers", {"category": category, "symbol": symbol}, signed=False)
        items = result.get("list") or []
        if not items:
            raise ExchangeHTTPError(400, f"no ticker for {symbol}", 10001, venue=self.venue)
        return Decimal(str(items[0]["lastPrice"]))

    # ── orders ────────────────────────────────────────────────────────────────
    async def place_order(self, symbol: str, side: str, order_type: str, qty: str,
                          price: Optional[str] = None, category: str = "spot",
                          client_order_id: Optional[str] = None) -> Dict[str, Any]:
        return await self._request("POST", "/v5/order/create", {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "price": price,
            "timeInForce": "GTC" if order_type == "Limit" else None,
            # Spot Market BUY qty defaults to QUOTE amount; we always size in BASE units.
            "marketUnit": "baseCoin" if (category == "spot" and order_type == "Market") else None,
            "orderLinkId": client_order_id,
        })

    async def get_order(self, symbol: str, client_order_id: str, category: str = "spot") -> Optional[VenueOrder]:
        """Find an order by orderLinkId: realtime (open + recent closed), then history."""
        for path, extra in (("/v5/order/realtime", {}), ("/v5/order/history", {})):
            try:
                result = await self._request("GET", path, {
                    "category": category, "symbol": symbol, "orderLinkId": client_order_id, **extra})
            except ExchangeHTTPError as exc:
                if exc.is_not_found:
                    continue
                raise
            items = result.get("list") or []
            if items:
                return self.parse_order(items[0])
        return None

    def parse_order(self, o: Dict[str, Any]) -> VenueOrder:
        executed = Decimal(str(o.get("cumExecQty", "0") or "0"))
        avg = o.get("avgPrice")
        return VenueOrder(self.venue, str(o.get("orderId", "")), str(o.get("orderLinkId", "")),
                          str(o.get("orderStatus", "")), executed,
                          Decimal(str(avg)) if avg not in (None, "", "0") else None, o)

    async def cancel_order(self, symbol: str, order_id: Optional[str] = None,
                           client_order_id: Optional[str] = None, category: str = "spot") -> Dict[str, Any]:
        return await self._request("POST", "/v5/order/cancel", {
            "category": category, "symbol": symbol, "orderId": order_id, "orderLinkId": client_order_id})

    async def open_orders(self, symbol: str, category: str = "spot") -> List[VenueOrder]:
        result = await self._request("GET", "/v5/order/realtime", {"category": category, "symbol": symbol})
        return [self.parse_order(o) for o in (result.get("list") or [])]

    async def symbol_rules(self, symbol: str, category: str = "spot") -> SymbolRules:
        result = await self._request("GET", "/v5/market/instruments-info",
                                     {"category": category, "symbol": symbol}, signed=False)
        items = result.get("list") or []
        if not items:
            raise ExchangeHTTPError(400, f"unknown symbol {symbol}", 10001, venue=self.venue)
        it = items[0]
        D = lambda v: Decimal(str(v)) if v not in (None, "") else None  # noqa: E731
        lot, price = it.get("lotSizeFilter", {}), it.get("priceFilter", {})
        # minOrderQty/maxOrderQty/maxOrderAmt are deprecated and not enforced by Bybit
        return SymbolRules(
            self.venue, symbol,
            tick_size=D(price.get("tickSize")),
            step_size=D(lot.get("basePrecision")),
            market_step_size=D(lot.get("basePrecision")),
            max_qty=D(lot.get("maxLimitOrderQty")),
            market_max_qty=D(lot.get("maxMarketOrderQty")),
            min_notional=D(lot.get("minOrderAmt")),
            trading=(it.get("status", "Trading") == "Trading"),
        )
