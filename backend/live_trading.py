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
- S2-03: send_order() / fetch_order() / cancel_venue_order() via exchange REST APIs (orders go through order_service)
- S2-04: sync_positions() via exchange account queries
- S2-05: subscribe_ticker() with exponential-backoff reconnect
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import re
import time
from decimal import Decimal

from exchange_clients import (
    BinanceRestClient,
    BybitRestClient,
    ExchangeError,
    ExchangeHTTPError,
    SymbolRules,
    VenueOrder,
)

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
    environment: str = "live"      # Binance: "demo" (fake funds) or "live"; other venues are always "live"
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
                    "environment": v.environment,
                    "connection_id": v.connection_id,
                }
                for k, v in self._connections.items()
            },
        }

    # ── Adapter connections ───────────────────────────────────────────────────

    @staticmethod
    def _make_binance_spot_api(api_key: str, api_secret: str, environment: str = "demo") -> BinanceRestClient:
        """Build a signed Binance Spot REST client for the demo or production exchange."""
        return BinanceRestClient(api_key, api_secret, environment=environment)

    @staticmethod
    def _make_bybit_account_api(api_key: str, api_secret: str) -> BybitRestClient:
        """Build a signed Bybit v5 REST client."""
        return BybitRestClient(api_key, api_secret)

    async def connect_binance(self, api_key: str, api_secret: str, environment: str = "demo") -> Dict[str, Any]:
        """
        Connect Binance Spot adapter using the signed REST client (Demo Mode by default).
        Verifies credentials via _verify_binance_credentials().
        """
        async with self._lock:
            if not api_key or not api_secret:
                raise ConnectionError("api_key and api_secret are required")

            spot_api = self._make_binance_spot_api(api_key, api_secret, environment)
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
                environment=environment,
                binance_spot_api=spot_api,
            )
            self._is_active = True
            return {
                "success": True,
                "connection_id": connection_id,
                "environment": environment,
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

    async def restore_connections(self) -> None:
        """
        Re-establish exchange connections after a restart from the stored (encrypted) credentials,
        so the reconciler can finish following open live orders. Best-effort: a failure is logged and
        leaves that venue disconnected (live orders on it then refuse with 503; they never fall back).
        """
        import database
        from credential_utils import decrypt_credential

        for adapter_id in ("binance", "binance_futures", "bybit"):
            try:
                cfg = await database.get_adapter_config(adapter_id)
                if not cfg or cfg.get("status") not in ("connected", "connected_offline"):
                    continue
                key = decrypt_credential(cfg.get("api_key") or "")
                secret = decrypt_credential(cfg.get("api_secret") or "")
                if not key or not secret:
                    logger.warning("cannot restore %s: stored credentials are unreadable (wrong ENCRYPTION_KEY?)", adapter_id)
                    continue
                if adapter_id == "bybit":
                    await self.connect_bybit(key, secret)
                else:
                    env = "demo"
                    try:
                        env = json.loads(cfg.get("extra_config") or "{}").get("environment", "demo")
                    except (TypeError, ValueError):
                        pass
                    await self.connect_binance(key, secret, environment=env)
                logger.info("restored %s connection", adapter_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not restore %s: %s", adapter_id, exc)

    async def disconnect(self, adapter_id: str) -> Dict[str, Any]:
        async with self._lock:
            conn = self._connections.get(adapter_id)
            if conn:
                conn.status = "disconnected"
            if not self.is_connected():
                self._is_active = False
            return {"success": True}

    # ── Venue routing ─────────────────────────────────────────────────────────

    _VENUE_OF_ADAPTER = {"binance": "BINANCE", "binance_futures": "BINANCE", "bybit": "BYBIT"}
    _SYMBOL_RE = re.compile(r"^[A-Z0-9]{4,20}$")
    _RULES_TTL = 600.0  # seconds

    @staticmethod
    def to_venue_symbol(instrument: str) -> str:
        """'BTC/USDT', 'btcusdt' or 'BTCUSDT.BINANCE' -> 'BTCUSDT'; raises ValueError if not a spot symbol."""
        parts = (instrument or "").split(".")
        if len(parts) > 2 or (len(parts) == 2 and parts[1].upper() not in ("BINANCE", "BYBIT")):
            raise ValueError(f"'{instrument}' is not a valid exchange symbol (unknown venue suffix)")
        sym = parts[0].replace("/", "").replace("-", "").replace("_", "").upper()
        if not LiveTradingManager._SYMBOL_RE.match(sym):
            raise ValueError(f"'{instrument}' is not a valid exchange symbol")
        return sym

    def environments(self) -> Dict[str, str]:
        """Environment ("demo" / "live") of each verified venue connection."""
        out: Dict[str, str] = {}
        for adapter_id, conn in self._connections.items():
            venue = self._VENUE_OF_ADAPTER.get(adapter_id)
            if venue and conn.status == "connected":
                out[venue] = conn.environment
        return out

    def ready_venues(self) -> List[str]:
        """Venues with a *verified* connection (credentials accepted by the exchange)."""
        venues = []
        for adapter_id, conn in self._connections.items():
            venue = self._VENUE_OF_ADAPTER.get(adapter_id)
            if venue and conn.status == "connected" and venue not in venues:
                venues.append(venue)
        return venues

    def resolve_venue(self, requested: Optional[str] = None) -> str:
        """
        Pick the venue for an order. A requested venue must be verified-connected; with none
        requested exactly one verified venue must exist (never silently "the first adapter").
        """
        ready = self.ready_venues()
        if requested:
            venue = requested.upper()
            if venue not in ready:
                raise RuntimeError(f"{venue} is not connected/verified")
            return venue
        if not ready:
            raise RuntimeError("No verified exchange connection. Connect an adapter first.")
        if len(ready) > 1:
            raise RuntimeError(f"Several venues connected ({', '.join(ready)}): specify the venue")
        return ready[0]

    def _client_for(self, venue: str, allow_offline: bool = False) -> Any:
        """
        REST client of a venue. New orders need a *verified* connection; looking orders up and cancelling
        them (reconcile, kill switch) also work through an unverified one, so a venue that was unreachable
        at startup never leaves live orders unmanaged.
        """
        ok = ("connected", "connected_offline") if allow_offline else ("connected",)
        for adapter_id, conn in self._connections.items():
            if self._VENUE_OF_ADAPTER.get(adapter_id) == venue and conn.status in ok:
                return conn.binance_spot_api if venue == "BINANCE" else conn.bybit_account_api
        raise RuntimeError(f"{venue} is not connected/verified")

    async def reverify_offline(self) -> None:
        """Retry credential verification for connections that were unreachable when established."""
        for adapter_id, conn in list(self._connections.items()):
            if conn.status != "connected_offline":
                continue
            client = conn.binance_spot_api or conn.bybit_account_api
            try:
                await client.account_info()
                conn.status = "connected"
                logger.info("%s is reachable again: connection verified", adapter_id)
            except ExchangeHTTPError as exc:
                if exc.is_auth:
                    conn.status = "error"
                    logger.error("%s credentials rejected on re-verification: %s", adapter_id, exc)
                    import ops_alerts
                    await ops_alerts.enqueue("venue_credentials_rejected",
                                             f"{adapter_id}: exchange rejected the API credentials",
                                             dedupe_key=f"cred:{adapter_id}", dedupe_seconds=3600)
            except ExchangeError:
                pass  # still unreachable: try again next round

    # ── Venue data used for pre-trade checks ──────────────────────────────────

    async def rules(self, venue: str, symbol: str) -> SymbolRules:
        """Symbol filters (cached). A failure to load them raises: orders are not sent unvalidated."""
        cache = self.__dict__.setdefault("_rules_cache", {})
        hit = cache.get((venue, symbol))
        if hit and time.monotonic() - hit[0] < self._RULES_TTL:
            return hit[1]
        rules = await self._client_for(venue).symbol_rules(symbol)
        cache[(venue, symbol)] = (time.monotonic(), rules)
        return rules

    async def reference_price(self, venue: str, symbol: str) -> Decimal:
        return await self._client_for(venue).ticker_price(symbol)

    _STABLES = frozenset({"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI", "USD"})

    async def equity_usdt(self, venue: str) -> Optional[Decimal]:
        """
        Account equity in USDT (mark-to-market), or None if it cannot be determined.
        Binance: balances valued with the USDT (or BTC-cross) price; unvaluable dust is ignored.
        Bybit: the unified account's totalEquity. Deposits/withdrawals change this number — the
        daily-loss check treats a withdrawal as a loss (safe direction) and a deposit as a gain.
        """
        try:
            client = self._client_for(venue)
            if venue == "BYBIT":
                result = await client.wallet_balance()
                lst = result.get("list") or []
                return Decimal(str(lst[0]["totalEquity"])) if lst and lst[0].get("totalEquity") not in (None, "") else None
            info = await client.account_info()
            prices = await client.all_ticker_prices()
            btc_usdt = prices.get("BTCUSDT")
            total = Decimal(0)
            for b in info.get("balances", []):
                amount = Decimal(str(b.get("free", "0") or "0")) + Decimal(str(b.get("locked", "0") or "0"))
                if amount <= 0:
                    continue
                asset = str(b.get("asset", ""))
                if asset in self._STABLES:
                    total += amount
                elif f"{asset}USDT" in prices:
                    total += amount * prices[f"{asset}USDT"]
                elif f"{asset}BTC" in prices and btc_usdt:
                    total += amount * prices[f"{asset}BTC"] * btc_usdt
            return total
        except Exception as exc:  # noqa: BLE001 - any failure means "unknown": the risk check refuses
            logger.warning("equity lookup failed (%s): %s", venue, exc)
            return None

    # ── Order management (via exchange REST APIs) ─────────────────────────────

    async def send_order(
        self,
        venue: str,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: str,
        price: Optional[str],
        client_order_id: str,
    ) -> Dict[str, Any]:
        """
        Send ONE order attempt carrying our client order id. Raises ExchangeHTTPError /
        ExchangeTransportError / ExchangeRateLimited; the caller classifies the outcome
        (`.outcome` = rejected | unknown | not_sent) and reconciles — this method never retries.
        """
        client = self._client_for(venue)
        side_u, type_u = side.upper(), order_type.upper()
        if side_u not in ("BUY", "SELL"):
            raise ValueError(f"Unsupported order side: {side}")
        if type_u not in ("MARKET", "LIMIT"):
            raise ValueError(f"Unsupported order type: {order_type}")
        if venue == "BINANCE":
            body = await client.new_order(
                symbol=symbol, side=side_u, order_type=type_u, quantity=qty, price=price,
                time_in_force="GTC" if type_u == "LIMIT" else None, client_order_id=client_order_id)
            vo = client.parse_order(body)
            return {"venue_order_id": vo.venue_order_id, "client_order_id": vo.client_order_id or client_order_id,
                    "raw_status": vo.raw_status, "executed_qty": vo.executed_qty, "avg_price": vo.avg_price}
        body = await client.place_order(
            symbol=symbol, side=side_u.capitalize(), order_type=type_u.capitalize(), qty=qty, price=price,
            client_order_id=client_order_id)
        # Bybit acknowledges acceptance only (asynchronous): status is learned by querying
        return {"venue_order_id": str(body.get("orderId", "")),
                "client_order_id": str(body.get("orderLinkId", "")) or client_order_id,
                "raw_status": "New", "executed_qty": Decimal(0), "avg_price": None}

    async def fetch_order(self, venue: str, symbol: str, client_order_id: str) -> Optional[VenueOrder]:
        """Look an order up by OUR client order id. None = the venue says it does not exist."""
        return await self._client_for(venue, allow_offline=True).get_order(symbol, client_order_id)

    async def cancel_venue_order(
        self, venue: str, symbol: str, *, client_order_id: str, order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        client = self._client_for(venue, allow_offline=True)
        return await client.cancel_order(symbol, order_id=order_id, client_order_id=None if order_id else client_order_id)

    async def free_balance(self, venue: str, asset: str) -> Decimal:
        """Freely available (not locked) balance of one asset, read fresh from the venue."""
        client = self._client_for(venue, allow_offline=True)
        if venue == "BYBIT":
            result = await client.wallet_balance()
            for wallet in result.get("list", []):
                for coin in wallet.get("coin", []):
                    if coin.get("coin") == asset:
                        avail = coin.get("availableToWithdraw") or coin.get("walletBalance") or "0"
                        return Decimal(str(avail or "0"))
            return Decimal(0)
        info = await client.account_info()
        for b in info.get("balances", []):
            if b.get("asset") == asset:
                return Decimal(str(b.get("free", "0") or "0"))
        return Decimal(0)

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
            asset = str(balance.get("asset", ""))
            if total > 0 and asset not in self._STABLES:
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
                asset = str(coin.get("coin", ""))
                if total > 0 and asset not in self._STABLES:
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
    Apply an order status update received from an exchange stream/poll to our order row.

    Matches on OUR client order id (Binance `c`/`clientOrderId`, Bybit `orderLinkId`) and goes through
    the lifecycle state machine, so a late or duplicate message can never move a terminal order.
    """
    import order_state
    import order_store

    client_order_id = update.get("clientOrderId") or update.get("c") or update.get("orderLinkId") or ""
    venue = str(update.get("venue", "BINANCE"))
    new_status = order_state.from_venue_status(venue, update.get("status") or update.get("orderStatus"))
    if not client_order_id or new_status is None:
        return
    order = await order_store.get_by_client_id(client_order_id)
    if not order:
        return
    try:
        filled = float(update.get("executedQty") or update.get("cumExecQty") or 0)
        await order_store.transition(
            order["id"], new_status, event="venue_update", detail={"source": "update"},
            filled_qty=filled, exchange_order_id=str(update.get("orderId") or order.get("exchange_order_id") or ""),
        )
    except order_state.InvalidTransition:
        logger.info("Ignoring stale update for %s", order["id"])
