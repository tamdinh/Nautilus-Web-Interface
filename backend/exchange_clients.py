"""
Minimal signed REST clients for Binance Spot and Bybit (v5).

NautilusTrader 2.x no longer exposes standalone Python HTTP clients for these
exchanges (its adapters are built around TradingNode), so LiveTradingManager
talks to the exchange REST APIs directly via httpx.

Base URLs can be overridden with BINANCE_BASE_URL / BYBIT_BASE_URL, e.g. to point
at testnet (https://testnet.binance.vision, https://api-testnet.bybit.com).
"""

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx


class ExchangeHTTPError(Exception):
    """Exchange returned an error. `status` is the HTTP status, `code` the exchange code."""

    def __init__(self, status: int, message: str, code: Optional[int] = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.code = code


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class BinanceRestClient:
    """Binance Spot signed REST client (HMAC-SHA256)."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        recv_window: int = 5000,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = (base_url or os.getenv("BINANCE_BASE_URL", "https://api.binance.com")).rstrip("/")
        self._timeout = timeout
        self._recv_window = recv_window

    async def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, signed: bool = True) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self._recv_window
            query = urlencode(params)
            query += "&signature=" + _sign(self._api_secret, query)
        else:
            query = urlencode(params)
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(method, url, headers={"X-MBX-APIKEY": self._api_key})
        try:
            body = resp.json()
        except ValueError:
            raise ExchangeHTTPError(resp.status_code, "invalid JSON response from Binance")
        if resp.status_code >= 400:
            raise ExchangeHTTPError(resp.status_code, str(body.get("msg", body)) if isinstance(body, dict) else str(body),
                                    body.get("code") if isinstance(body, dict) else None)
        return body

    async def account_info(self) -> Dict[str, Any]:
        return await self._request("GET", "/api/v3/account")

    async def new_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        price: Optional[str] = None,
        time_in_force: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._request("POST", "/api/v3/order", {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
            "price": price,
            "timeInForce": time_in_force,
        })

    async def cancel_order(
        self, symbol: str, order_id: Optional[int] = None, orig_client_order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        return await self._request("DELETE", "/api/v3/order", {
            "symbol": symbol,
            "orderId": order_id,
            "origClientOrderId": orig_client_order_id,
        })


class BybitRestClient:
    """Bybit v5 signed REST client (HMAC-SHA256)."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        recv_window: int = 5000,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = (base_url or os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")).rstrip("/")
        self._timeout = timeout
        self._recv_window = str(recv_window)

    async def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        ts = str(int(time.time() * 1000))
        if method == "GET":
            payload = urlencode(params)
            url = f"{self._base_url}{path}" + (f"?{payload}" if payload else "")
            content = None
        else:
            payload = json.dumps(params, separators=(",", ":"))
            url = f"{self._base_url}{path}"
            content = payload
        headers = {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": self._recv_window,
            "X-BAPI-SIGN": _sign(self._api_secret, ts + self._api_key + self._recv_window + payload),
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(method, url, headers=headers, content=content)
        try:
            body = resp.json()
        except ValueError:
            raise ExchangeHTTPError(resp.status_code, "invalid JSON response from Bybit")
        if resp.status_code >= 400:
            raise ExchangeHTTPError(resp.status_code, str(body))
        # Bybit reports API errors with HTTP 200 and a non-zero retCode
        ret_code = body.get("retCode", 0) if isinstance(body, dict) else 0
        if ret_code != 0:
            raise ExchangeHTTPError(resp.status_code, str(body.get("retMsg", body)), ret_code)
        return body.get("result", {})

    async def account_info(self) -> Dict[str, Any]:
        return await self._request("GET", "/v5/account/info")

    async def wallet_balance(self, account_type: str = "UNIFIED") -> Dict[str, Any]:
        return await self._request("GET", "/v5/account/wallet-balance", {"accountType": account_type})

    async def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        qty: str,
        price: Optional[str] = None,
        category: str = "spot",
    ) -> Dict[str, Any]:
        return await self._request("POST", "/v5/order/create", {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "price": price,
            "timeInForce": "GTC" if order_type == "Limit" else None,
        })

    async def cancel_order(self, symbol: str, order_id: str, category: str = "spot") -> Dict[str, Any]:
        return await self._request("POST", "/v5/order/cancel", {
            "category": category,
            "symbol": symbol,
            "orderId": order_id,
        })
