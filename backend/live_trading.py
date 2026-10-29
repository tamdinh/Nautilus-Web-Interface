"""
LiveTradingManager — Sprint 2 (S2-01 through S2-05).

Manages the lifecycle of live adapter connections and order routing.
Uses signed httpx REST clients (exchange_clients.py). NautilusTrader 2.x no longer
ships standalone Python HTTP clients for Binance/Bybit (its adapters are TradingNode
based), so the REST calls are made directly.

Architecture:
- BinanceRestClient → Binance Spot REST (HMAC-SHA256)
- BybitRestClient   → Bybit v5 REST (HMAC-SHA256)
- LiveTradingManager wraps these for FastAPI to call

State:
- S2-01: LiveTradingManager class with full interface
- S2-02: connect_binance() / connect_bybit() using REST clients
- S2-03: submit_order() / cancel_order() via exchange REST APIs
- S2-04: sync_positions() via exchange account queries
- S2-05: subscribe_ticker() with exponential-backoff reconnect
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from exchange_clients import BinanceRestClient, BybitRestClient, ExchangeHTTPError

logger = logging.getLogger(__name__)


class BinanceAuthError(ConnectionError):
    """Raised when Binance explicitly rejects credentials (HTTP 401/403)."""


class BybitAuthError(ConnectionError):
    """Raised when Bybit explicitly rejects credentials (HTTP 401/403)."""


@dataclass
class AdapterConnection:
    adapter_id: str
    connection_id: str
    status: str = "connected"
    node: Any = None  # reserved for future TradingNode reference
    # REST clients (set after successful connect)
    binance_spot_api: Any = None   # BinanceRestClient
    bybit_account_api: Any = None  # BybitRestClient


# Binance error codes meaning "bad / unauthorised API key or signature"
_BINANCE_AUTH_CODES = {-1022, -2014, -2015}
# Bybit retCodes meaning "bad key / signature / permission / IP"
_BYBIT_AUTH_CODES = {10003, 10004, 10005, 10007, 10010, 33004}


class LiveTradingManager:
    """
    Thread-safe manager for live adapter connections and order routing.

    Uses signed httpx REST clients for all exchange calls.
    All state-mutating methods are guarded by asyncio.Lock.
    """

    def __init__(self) -> None:
        self._connections: Dict[str, AdapterConnection] = {}
        self._is_active: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()
        self._order_callbacks: List[Callable] = []

    # ── Connection state ──────────────────────────────────────────────────────

    def is_connected(self, adapter_id: Optional[str] = None) -> bool:
        if adapter_id:
            conn = self._connections.get(adapter_id)
            return conn is not None and conn.status in ("connected", "connected_offline")
        return any(
            c.status in ("connected", "connected_offline")
            for c in self._connections.values()
        )

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_active": self._is_active,
            "connections": {
                k: {
                    "adapter_id": v.adapter_id,
                    "status": v.status,
                    "connection_id": v.connection_id,
                }
                for k, v in self._connections.items()
            },
        }

    # ── Adapter connections ───────────────────────────────────────────────────

    @staticmethod
    def _make_binance_spot_api(api_key: str, api_secret: str) -> BinanceRestClient:
        """Build a signed Binance Spot REST client."""
        return BinanceRestClient(api_key, api_secret)

    @staticmethod
    def _make_bybit_account_api(api_key: str, api_secret: str) -> BybitRestClient:
        """Build a signed Bybit v5 REST client."""
        return BybitRestClient(api_key, api_secret)

    async def connect_binance(self, api_key: str, api_secret: str) -> Dict[str, Any]:
        """
        Connect Binance Spot adapter using the signed REST client.
        Verifies credentials via _verify_binance_credentials().
        """
        async with self._lock:
            if not api_key or not api_secret:
                raise ConnectionError("api_key and api_secret are required")

            spot_api = self._make_binance_spot_api(api_key, api_secret)
            verified = False
            account_info: Dict[str, Any] = {}

            try:
                account_info = await self._verify_binance_credentials(
                    api_key, api_secret, spot_api=spot_api
                )
                verified = True
            except BinanceAuthError:
                raise
            except Exception:
                # Network / timeout → connected_offline
                pass

            connection_id = f"CONN-BINANCE-{uuid.uuid4().hex[:8].upper()}"
            status = "connected" if verified else "connected_offline"
            self._connections["binance"] = AdapterConnection(
                adapter_id="binance",
                connection_id=connection_id,
                status=status,
                binance_spot_api=spot_api,
            )
            self._is_active = True
            return {
                "success": True,
                "connection_id": connection_id,
                "verified": verified,
                "account_info": account_info,
            }

    async def _verify_binance_credentials(
        self,
        api_key: str,
        api_secret: str,
        spot_api: Any = None,
    ) -> Dict[str, Any]:
        """
        Verify Binance credentials via GET /api/v3/account.

        Kept as a named method so tests can patch it independently.
        Returns {"valid": True, "can_trade": bool, ...} on success.
        Raises BinanceAuthError if Binance rejects the credentials.
        Raises ConnectionError on network/timeout issues.
        """
        if spot_api is None:
            spot_api = self._make_binance_spot_api(api_key, api_secret)

        try:
            info = await spot_api.account_info()
            return {
                "valid": True,
                "can_trade": bool(info.get("canTrade", False)),
                "can_withdraw": bool(info.get("canWithdraw", False)),
                "account_type": info.get("accountType", "SPOT"),
            }
        except ExchangeHTTPError as exc:
            # Only treat as hard auth failure when Binance explicitly rejects
            # the credentials; everything else is a connectivity/other issue.
            if exc.status in (401, 403) or exc.code in _BINANCE_AUTH_CODES:
                raise BinanceAuthError(f"Binance rejected credentials: {exc}") from exc
            raise ConnectionError(f"Binance API error: {exc}") from exc
        except Exception as exc:
            raise ConnectionError(f"Binance API unreachable: {exc}") from exc

    async def connect_bybit(self, api_key: str, api_secret: str) -> Dict[str, Any]:
        """
        Connect Bybit adapter using the signed REST client.
        Verifies credentials via account_info().
        """
        async with self._lock:
            if not api_key or not api_secret:
                raise ConnectionError("api_key and api_secret are required")

            bybit_api = self._make_bybit_account_api(api_key, api_secret)
            verified = False
            account_info: Dict[str, Any] = {}

            try:
                info = await bybit_api.account_info()
                verified = True
                account_info = {
                    "unified_margin_status": info.get("unifiedMarginStatus"),
                    "account_type": "UNIFIED",
                }
            except ExchangeHTTPError as exc:
                if exc.status in (401, 403) or exc.code in _BYBIT_AUTH_CODES:
                    raise BybitAuthError(
                        f"Bybit rejected credentials: {exc}"
                    ) from exc
                # Other API error → connected_offline
            except Exception:
                # Network / timeout → connected_offline
                pass

            connection_id = f"CONN-BYBIT-{uuid.uuid4().hex[:8].upper()}"
            status = "connected" if verified else "connected_offline"
            self._connections["bybit"] = AdapterConnection(
                adapter_id="bybit",
                connection_id=connection_id,
                status=status,
                bybit_account_api=bybit_api,
            )
            self._is_active = True
            return {
                "success": True,
                "connection_id": connection_id,
                "verified": verified,
                "account_info": account_info,
            }

    async def disconnect(self, adapter_id: str) -> Dict[str, Any]:
        async with self._lock:
            conn = self._connections.get(adapter_id)
            if conn:
                conn.status = "disconnected"
            if not self.is_connected():
                self._is_active = False
            return {"success": True}

    # ── Order management (via exchange REST APIs) ─────────────────────

    async def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """
        Submit an order via the connected exchange's REST API.
        Routes to Binance or Bybit based on active connection.
        """
        if not self.is_connected():
            raise RuntimeError("No adapter connected. Connect an exchange adapter first.")

        for adapter_id, conn in self._connections.items():
            if conn.status not in ("connected", "connected_offline"):
                continue
            try:
                if adapter_id in ("binance", "binance_futures") and conn.binance_spot_api:
                    return await self._submit_binance_order(conn, order)
                elif adapter_id == "bybit" and conn.bybit_account_api:
                    return await self._submit_bybit_order(conn, order)
            except Exception as exc:
                logger.warning("Exchange order submission failed (%s): %s", adapter_id, exc)
                raise RuntimeError(str(exc)) from exc

        raise RuntimeError("No active exchange connection available.")

    async def _submit_binance_order(
        self, conn: AdapterConnection, order: Dict[str, Any]
    ) -> Dict[str, Any]:
        symbol = order.get("instrument", "BTCUSDT").replace("/", "").split(".")[0]
        side = order.get("side", "BUY").upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"Unsupported order side: {side}")
        order_type = order.get("type", "MARKET").upper()
        if order_type not in ("MARKET", "LIMIT"):
            raise ValueError(f"Unsupported order type: {order_type}")
        quantity = str(order.get("quantity", 0.001))
        price = str(order.get("price")) if order.get("price") else None
        if order_type == "LIMIT" and price is None:
            raise ValueError("Limit orders require a price")

        result = await conn.binance_spot_api.new_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            time_in_force="GTC" if order_type == "LIMIT" else None,
        )

        order_id = str(result.get("orderId", ""))
        return {
            "success": True,
            "order_id": order_id,
            "exchange_order_id": order_id,
            "client_order_id": str(result.get("clientOrderId", "")),
            "status": str(result.get("status", "NEW")).lower(),
            "exchange": "BINANCE",
        }

    async def _submit_bybit_order(
        self, conn: AdapterConnection, order: Dict[str, Any]
    ) -> Dict[str, Any]:
        symbol = order.get("instrument", "BTCUSDT").replace("/", "").split(".")[0]
        side = order.get("side", "BUY").capitalize()
        if side not in ("Buy", "Sell"):
            raise ValueError(f"Unsupported order side: {side}")
        order_type = order.get("type", "Market").capitalize()
        if order_type not in ("Market", "Limit"):
            raise ValueError(f"Unsupported order type: {order_type}")
        quantity = str(order.get("quantity", 0.001))
        price = str(order.get("price")) if order.get("price") else None
        if order_type == "Limit" and price is None:
            raise ValueError("Limit orders require a price")

        result = await conn.bybit_account_api.place_order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            qty=quantity,
            price=price,
        )

        order_id = str(result.get("orderId", ""))
        return {
            "success": True,
            "order_id": order_id,
            "exchange_order_id": order_id,
            "status": "pending",
            "exchange": "BYBIT",
        }

    async def cancel_order(self, order_id: str, symbol: str = "BTCUSDT") -> Dict[str, Any]:
        """Cancel an order on the connected exchange via REST API."""
        if not self.is_connected():
            raise RuntimeError("No adapter connected.")

        for adapter_id, conn in self._connections.items():
            if conn.status not in ("connected", "connected_offline"):
                continue
            try:
                if adapter_id in ("binance", "binance_futures") and conn.binance_spot_api:
                    result = await conn.binance_spot_api.cancel_order(
                        symbol=symbol, order_id=int(order_id) if order_id.isdigit() else None,
                        orig_client_order_id=None if order_id.isdigit() else order_id,
                    )
                    return {
                        "success": True,
                        "order_id": order_id,
                        "status": str(result.get("status", "CANCELED")).lower(),
                    }
                elif adapter_id == "bybit" and conn.bybit_account_api:
                    await conn.bybit_account_api.cancel_order(
                        symbol=symbol,
                        order_id=order_id,
                    )
                    return {"success": True, "order_id": order_id}
            except Exception as exc:
                logger.warning("Exchange cancel order failed (%s): %s", adapter_id, exc)
                return {"success": False, "order_id": order_id, "error": str(exc)}

        return {"success": True, "order_id": order_id}

    async def sync_positions(self) -> List[Dict[str, Any]]:
        """
        Fetch account balances/positions from the connected exchange
        via the exchange REST APIs.
        """
        if not self.is_connected():
            return []

        all_positions: List[Dict[str, Any]] = []
        for adapter_id, conn in self._connections.items():
            if conn.status not in ("connected", "connected_offline"):
                continue
            try:
                if adapter_id in ("binance", "binance_futures") and conn.binance_spot_api:
                    positions = await self._fetch_binance_positions(conn)
                elif adapter_id == "bybit" and conn.bybit_account_api:
                    positions = await self._fetch_bybit_positions(conn)
                else:
                    positions = []
                all_positions.extend(positions)
            except Exception as exc:
                logger.warning("Position sync failed (%s): %s", adapter_id, exc)

        return all_positions

    async def _fetch_binance_positions(self, conn: AdapterConnection) -> List[Dict[str, Any]]:
        """Query Binance SPOT account and return non-zero balances as positions."""
        info = await conn.binance_spot_api.account_info()
        positions = []
        for balance in info.get("balances", []):
            free = float(balance.get("free", 0) or 0)
            locked = float(balance.get("locked", 0) or 0)
            total = free + locked
            if total > 0:
                asset = str(balance.get("asset", ""))
                positions.append({
                    "instrument": f"{asset}USDT",
                    "side": "LONG",
                    "quantity": total,
                    "free": free,
                    "locked": locked,
                    "source": "live",
                    "exchange": "BINANCE",
                })
        return positions

    async def _fetch_bybit_positions(self, conn: AdapterConnection) -> List[Dict[str, Any]]:
        """Query Bybit wallet balance and return non-zero coins as positions."""
        result = await conn.bybit_account_api.wallet_balance()
        positions = []
        for wallet in result.get("list", []):
            for coin in wallet.get("coin", []):
                total = float(coin.get("walletBalance", 0) or 0)
                if total > 0:
                    asset = str(coin.get("coin", ""))
                    positions.append({
                        "instrument": f"{asset}USDT",
                        "side": "LONG",
                        "quantity": total,
                        "equity": float(coin.get("equity", total) or total),
                        "source": "live",
                        "exchange": "BYBIT",
                    })
        return positions

    # ── Market data WebSocket ─────────────────────────────────────────────────

    async def _connect_ws(
        self, symbol: str, on_message: Callable, backoff: float = 1.0
    ) -> None:
        """
        Open a single WebSocket session to the Binance ticker stream.
        Raises on disconnect (caller handles reconnect).
        """
        import websockets  # type: ignore

        url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@ticker"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            async for raw in ws:
                data = json.loads(raw)
                await on_message(data)

    async def subscribe_ticker(
        self, symbol: str, on_message: Callable, backoff: float = 1.0
    ) -> None:
        """
        Subscribe to Binance WebSocket ticker stream with exponential-backoff reconnect.
        Runs forever — cancel the task to stop.
        """
        max_backoff = 60.0
        current_backoff = backoff

        while True:
            try:
                await self._connect_ws(symbol, on_message, backoff=current_backoff)
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(current_backoff)
                current_backoff = min(current_backoff * 2, max_backoff)


async def process_order_update(update: Dict[str, Any]) -> None:
    """
    Process an order status update received from the exchange WebSocket.
    Updates the order status in the DB.
    """
    import database

    exchange_order_id = update.get("orderId", "")
    status = update.get("status", "").lower()
    executed_qty = float(update.get("executedQty", 0))

    if not exchange_order_id:
        return

    status_map = {
        "filled": "filled",
        "partially_filled": "partial",
        "canceled": "CANCELLED",
        "cancelled": "CANCELLED",
        "rejected": "rejected",
        "pending": "PENDING",
        "new": "PENDING",
    }
    db_status = status_map.get(status, status)

    async with database.aiosqlite.connect(database.DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET status=?, filled_qty=? WHERE exchange_order_id=?",
            (db_status, executed_qty, str(exchange_order_id)),
        )
        await db.commit()
