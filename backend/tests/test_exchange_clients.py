"""
Exchange REST clients: signing, client order ids, outcome classification (rejected / unknown /
not_sent), clock sync, rate-limit gate, symbol rules, and pre-trade validation.

All HTTP traffic is served by httpx.MockTransport — nothing reaches a real exchange.
"""

import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import exchange_clients
from exchange_clients import (
    BinanceRestClient,
    BybitRestClient,
    ExchangeHTTPError,
    ExchangeRateLimited,
    ExchangeTransportError,
    SymbolRules,
)
from live_trading import BinanceAuthError, BybitAuthError, LiveTradingManager
from order_rules import OrderValidationError, validate_order

pytestmark = pytest.mark.asyncio

BASE_B = "https://binance.test"
BASE_Y = "https://bybit.test"


@pytest.fixture
def mock_http(monkeypatch):
    """Route every httpx.AsyncClient created by exchange_clients through a handler."""
    state = {"handler": None, "requests": []}
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        def handler(request):
            state["requests"].append(request)
            return state["handler"](request)

        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(exchange_clients.httpx, "AsyncClient", factory)
    return state


def _binance(**kw):
    c = BinanceRestClient("key12345", "secret12345", base_url=BASE_B, **kw)
    c._synced = True  # skip the /time round-trip unless a test is about it
    return c


def _bybit(**kw):
    c = BybitRestClient("key12345", "secret12345", base_url=BASE_Y, **kw)
    c._synced = True
    return c


def _query(req):
    return parse_qs(urlparse(str(req.url)).query)


# ── Binance: signing, ids ────────────────────────────────────────────────────

async def test_binance_account_info_is_signed(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"canTrade": True, "balances": []})
    info = await _binance().account_info()
    assert info["canTrade"] is True
    req = mock_http["requests"][0]
    assert req.headers["X-MBX-APIKEY"] == "key12345"
    query = urlparse(str(req.url)).query
    unsigned, _, sig = query.rpartition("&signature=")
    assert sig == hmac.new(b"secret12345", unsigned.encode(), hashlib.sha256).hexdigest()
    assert "timestamp=" in unsigned and "recvWindow=5000" in unsigned


async def test_binance_new_order_carries_client_order_id(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={
        "orderId": 42, "clientOrderId": "nwabc", "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0"})
    mgr = LiveTradingManager()
    mgr._connections["binance"] = type("C", (), {"status": "connected", "binance_spot_api": _binance(), "bybit_account_api": None})()
    res = await mgr.send_order("BINANCE", symbol="BTCUSDT", side="buy", order_type="limit", qty="0.5",
                               price="100", client_order_id="nwabc")
    assert res["venue_order_id"] == "42" and res["raw_status"] == "NEW"
    q = _query(mock_http["requests"][0])
    assert q["newClientOrderId"] == ["nwabc"] and q["timeInForce"] == ["GTC"] and q["type"] == ["LIMIT"]
    assert q["symbol"] == ["BTCUSDT"] and q["side"] == ["BUY"] and q["quantity"] == ["0.5"] and q["price"] == ["100"]


async def test_binance_get_order_not_found_returns_none(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."})
    assert await _binance().get_order("BTCUSDT", client_order_id="nwabc") is None


async def test_binance_get_order_parses_fill_and_average_price(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={
        "orderId": 7, "clientOrderId": "nwabc", "status": "PARTIALLY_FILLED",
        "executedQty": "0.5", "cummulativeQuoteQty": "15000"})
    vo = await _binance().get_order("BTCUSDT", client_order_id="nwabc")
    assert vo.raw_status == "PARTIALLY_FILLED" and vo.executed_qty == Decimal("0.5")
    assert vo.avg_price == Decimal("30000")
    assert _query(mock_http["requests"][0])["origClientOrderId"] == ["nwabc"]


# ── Binance: outcome classification ──────────────────────────────────────────

@pytest.mark.parametrize("status,code,expected", [
    (400, -1013, "rejected"),     # filter failure: never reached the matching engine
    (400, -2010, "rejected"),     # matching-engine rejection (e.g. insufficient balance)
    (400, -1121, "rejected"),     # invalid symbol
    (401, -2015, "rejected"),     # bad key
    (503, None, "unknown"),       # 5xx: execution status unknown
    (500, -1007, "unknown"),      # backend timeout: execution status unknown
    (200, -1006, "unknown"),
    (429, -1003, "unknown"),      # docs do not promise a 429 means "not processed"
    (418, None, "unknown"),
])
async def test_binance_error_outcome_classification(mock_http, status, code, expected):
    body = {"code": code, "msg": "x"} if code is not None else {"msg": "x"}
    mock_http["handler"] = lambda req: httpx.Response(status if status >= 400 else 400, json=body)
    with pytest.raises(ExchangeHTTPError) as ei:
        await _binance().new_order("BTCUSDT", "BUY", "MARKET", "1", client_order_id="nw1")
    assert ei.value.outcome == expected


@pytest.mark.parametrize("status,code", [(400, -1000), (400, -1001), (400, None), (408, None), (404, None), (400, -9999)])
async def test_binance_unlisted_or_ambiguous_errors_are_unknown_not_rejected(mock_http, status, code):
    body = {"code": code, "msg": "x"} if code is not None else {"msg": "x"}
    mock_http["handler"] = lambda req: httpx.Response(status, json=body)
    with pytest.raises(ExchangeHTTPError) as ei:
        await _binance().new_order("BTCUSDT", "BUY", "MARKET", "1", client_order_id="nw1")
    assert ei.value.outcome == "unknown"


async def test_binance_invalid_json_is_unknown(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, content=b"<html>not json</html>")
    with pytest.raises(ExchangeHTTPError) as ei:
        await _binance().new_order("BTCUSDT", "BUY", "MARKET", "1", client_order_id="nw1")
    assert ei.value.outcome == "unknown"


async def test_transport_errors_distinguish_not_sent_from_maybe_sent(mock_http):
    def refuse(req):
        raise httpx.ConnectError("refused")

    def timeout(req):
        raise httpx.ReadTimeout("slow")

    mock_http["handler"] = refuse
    with pytest.raises(ExchangeTransportError) as ei:
        await _binance().new_order("BTCUSDT", "BUY", "MARKET", "1", client_order_id="nw1")
    assert ei.value.maybe_sent is False and ei.value.outcome == "not_sent"

    mock_http["handler"] = timeout
    with pytest.raises(ExchangeTransportError) as ei:
        await _binance().new_order("BTCUSDT", "BUY", "MARKET", "1", client_order_id="nw1")
    assert ei.value.maybe_sent is True and ei.value.outcome == "unknown"


async def test_binance_rate_limit_blocks_following_requests_locally(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(429, json={"code": -1003, "msg": "slow down"},
                                                      headers={"Retry-After": "30"})
    client = _binance()
    with pytest.raises(ExchangeHTTPError):
        await client.account_info()
    sent = len(mock_http["requests"])
    with pytest.raises(ExchangeRateLimited) as ei:
        await client.new_order("BTCUSDT", "BUY", "MARKET", "1", client_order_id="nw1")
    assert len(mock_http["requests"]) == sent, "nothing may be sent while Retry-After is active"
    assert ei.value.outcome == "not_sent" and ei.value.retry_in > 20


async def test_binance_timestamp_error_resyncs_clock_and_retries_once(mock_http):
    calls = {"n": 0}

    def handler(req):
        path = urlparse(str(req.url)).path
        if path == "/api/v3/time":
            return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside of the recvWindow."})
        return httpx.Response(200, json={"canTrade": True})

    mock_http["handler"] = handler
    info = await _binance().account_info()
    assert info["canTrade"] is True and calls["n"] == 2
    assert any(urlparse(str(r.url)).path == "/api/v3/time" for r in mock_http["requests"])


async def test_binance_401_maps_to_auth_error_in_verify(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key"})
    mgr = LiveTradingManager()
    with pytest.raises(BinanceAuthError):
        await mgr._verify_binance_credentials("key12345", "secret12345", spot_api=_binance())


async def test_binance_network_failure_is_connection_error_in_verify(mock_http):
    def boom(req):
        raise httpx.ConnectError("no route")

    mock_http["handler"] = boom
    mgr = LiveTradingManager()
    with pytest.raises(ConnectionError):
        await mgr._verify_binance_credentials("key12345", "secret12345", spot_api=_binance())


# ── Bybit ────────────────────────────────────────────────────────────────────

async def test_bybit_get_is_signed(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 0, "result": {"unifiedMarginStatus": 4}})
    info = await _bybit().account_info()
    assert info["unifiedMarginStatus"] == 4
    req = mock_http["requests"][0]
    ts = req.headers["X-BAPI-TIMESTAMP"]
    assert req.headers["X-BAPI-SIGN"] == hmac.new(b"secret12345", f"{ts}key12345{5000}".encode(), hashlib.sha256).hexdigest()


async def test_bybit_market_order_is_sized_in_base_units_with_link_id(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 0, "result": {"orderId": "9", "orderLinkId": "nw1"}})
    await _bybit().place_order("BTCUSDT", "Buy", "Market", "0.001", client_order_id="nw1")
    body = json.loads(mock_http["requests"][0].content)
    assert body["marketUnit"] == "baseCoin", "spot market BUY qty defaults to the QUOTE amount on Bybit"
    assert body["orderLinkId"] == "nw1" and body["category"] == "spot" and "price" not in body
    req = mock_http["requests"][0]
    ts = req.headers["X-BAPI-TIMESTAMP"]
    expected = hmac.new(b"secret12345", f"{ts}key12345{5000}{req.content.decode()}".encode(), hashlib.sha256).hexdigest()
    assert req.headers["X-BAPI-SIGN"] == expected


async def test_bybit_limit_order_has_gtc_and_no_market_unit(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 0, "result": {"orderId": "9"}})
    await _bybit().place_order("BTCUSDT", "Sell", "Limit", "1", price="100", client_order_id="nw2")
    body = json.loads(mock_http["requests"][0].content)
    assert body["timeInForce"] == "GTC" and body["price"] == "100" and "marketUnit" not in body


@pytest.mark.parametrize("ret_code,expected", [
    (170131, "rejected"),   # insufficient balance
    (170136, "rejected"),   # quantity below minimum
    (110017, "rejected"),   # UTA: order qty truncated to zero
    (10003, "rejected"),    # invalid key
    (10001, "rejected"),    # invalid parameters
    (10006, "unknown"),     # rate limit: not guaranteed to have created nothing
    (10016, "unknown"),     # server error
    (20001, "unknown"),     # a code we have never seen: must not be assumed to be a refusal
    (30084, "unknown"),
])
async def test_bybit_retcode_outcome_classification(mock_http, ret_code, expected):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": ret_code, "retMsg": "x"})
    with pytest.raises(ExchangeHTTPError) as ei:
        await _bybit().place_order("BTCUSDT", "Buy", "Market", "1", client_order_id="nw1")
    assert ei.value.outcome == expected


async def test_bybit_5xx_and_403_are_unknown_and_403_blocks(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(503, text="oops")
    with pytest.raises(ExchangeHTTPError) as ei:
        await _bybit().place_order("BTCUSDT", "Buy", "Market", "1", client_order_id="nw1")
    assert ei.value.outcome == "unknown"

    mock_http["handler"] = lambda req: httpx.Response(403, json={"retMsg": "access too frequent"})
    client = _bybit()
    with pytest.raises(ExchangeHTTPError):
        await client.account_info()
    with pytest.raises(ExchangeRateLimited):
        await client.account_info()


async def test_bybit_get_order_falls_back_from_realtime_to_history(mock_http):
    def handler(req):
        path = urlparse(str(req.url)).path
        if path == "/v5/order/realtime":
            return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
        return httpx.Response(200, json={"retCode": 0, "result": {"list": [
            {"orderId": "5", "orderLinkId": "nw1", "orderStatus": "Filled", "cumExecQty": "0.001", "avgPrice": "30000"}]}})

    mock_http["handler"] = handler
    vo = await _bybit().get_order("BTCUSDT", "nw1")
    assert vo.raw_status == "Filled" and vo.executed_qty == Decimal("0.001") and vo.avg_price == Decimal("30000")
    assert [urlparse(str(r.url)).path for r in mock_http["requests"]] == ["/v5/order/realtime", "/v5/order/history"]


async def test_bybit_get_order_unknown_everywhere_returns_none(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
    assert await _bybit().get_order("BTCUSDT", "nwX") is None


async def test_bybit_timestamp_error_resyncs_and_retries(mock_http):
    calls = {"n": 0}

    def handler(req):
        path = urlparse(str(req.url)).path
        if path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "result": {"timeSecond": "1700000000", "timeNano": "1700000000123456789"}})
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"retCode": 10002, "retMsg": "invalid request, please check your server timestamp"})
        return httpx.Response(200, json={"retCode": 0, "result": {"ok": 1}})

    mock_http["handler"] = handler
    assert (await _bybit().account_info()) == {"ok": 1}


async def test_bybit_retcode_auth_error_on_http_200(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 10003, "retMsg": "API key is invalid"})
    mgr = LiveTradingManager()
    mgr._make_bybit_account_api = lambda k, s: _bybit()
    with pytest.raises(BybitAuthError):
        await mgr.connect_bybit("key12345", "secret12345")


async def test_bybit_non_auth_error_connects_offline(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 10016, "retMsg": "server error"})
    mgr = LiveTradingManager()
    mgr._make_bybit_account_api = lambda k, s: _bybit()
    result = await mgr.connect_bybit("key12345", "secret12345")
    assert result["verified"] is False
    assert mgr.is_connected("bybit") and mgr.ready_venues() == []   # unverified never carries live orders


# ── Symbol rules + pre-trade validation ──────────────────────────────────────

async def test_binance_symbol_rules_parsed(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"symbols": [{
        "symbol": "BTCUSDT", "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000", "stepSize": "0.00001"},
            {"filterType": "NOTIONAL", "minNotional": "5", "applyMinToMarket": True, "maxNotional": "9000000"},
        ]}]})
    r = await _binance().symbol_rules("BTCUSDT")
    assert r.tick_size == Decimal("0.01") and r.step_size == Decimal("0.00001")
    assert r.min_notional == Decimal("5") and r.min_qty == Decimal("0.00001") and r.trading


async def test_bybit_symbol_rules_ignore_deprecated_fields(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 0, "result": {"list": [{
        "symbol": "BTCUSDT", "status": "Trading",
        "lotSizeFilter": {"basePrecision": "0.000001", "minOrderQty": "0.1", "maxOrderQty": "1", "minOrderAmt": "5",
                          "maxLimitOrderQty": "100", "maxMarketOrderQty": "50"},
        "priceFilter": {"tickSize": "0.1"}}]}})
    r = await _bybit().symbol_rules("BTCUSDT")
    assert r.step_size == Decimal("0.000001") and r.tick_size == Decimal("0.1")
    assert r.min_qty is None, "minOrderQty is deprecated/not enforced by Bybit: must not be applied"
    assert r.min_notional == Decimal("5") and r.max_qty == Decimal("100") and r.market_max_qty == Decimal("50")


RULES = SymbolRules("BINANCE", "BTCUSDT", tick_size=Decimal("0.01"), min_price=Decimal("0.01"),
                    step_size=Decimal("0.001"), min_qty=Decimal("0.001"), max_qty=Decimal("100"),
                    min_notional=Decimal("10"))


def test_validate_accepts_a_valid_limit_order_and_returns_exact_strings():
    q, p = validate_order(RULES, "BUY", "LIMIT", Decimal("0.5"), Decimal("30000.10"))
    assert (q, p) == ("0.5", "30000.10")


@pytest.mark.parametrize("qty,price,otype,ref,msg", [
    ("0.0005", "30000", "LIMIT", None, "minimum"),
    ("0.0015", "30000", "LIMIT", None, "step size"),
    ("200", "30000", "LIMIT", None, "maximum"),
    ("0.5", "30000.005", "LIMIT", None, "tick size"),
    ("0.001", "5", "LIMIT", None, "notional"),
    ("0.5", None, "LIMIT", None, "price"),
    ("0.5", "1", "MARKET", None, "must not carry a price"),
    ("0.5", None, "MARKET", None, "no price"),            # cannot value a market order: refuse
    ("0.0001", None, "MARKET", "30000", "minimum"),
])
def test_validate_rejects_violations(qty, price, otype, ref, msg):
    with pytest.raises(OrderValidationError, match=msg):
        validate_order(RULES, "BUY", otype, Decimal(qty), Decimal(price) if price else None,
                       Decimal(ref) if ref else None)


def test_validate_market_order_valued_with_reference_price():
    q, p = validate_order(RULES, "SELL", "MARKET", Decimal("0.5"), None, Decimal("30000"))
    assert (q, p) == ("0.5", None)


def test_validate_refuses_untradable_symbol():
    halted = SymbolRules("BINANCE", "BTCUSDT", trading=False)
    with pytest.raises(OrderValidationError, match="not currently tradable"):
        validate_order(halted, "BUY", "MARKET", Decimal("1"), None, Decimal("1"))


# ── Manager routing helpers ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("BTC/USDT", "BTCUSDT"), ("btcusdt", "BTCUSDT"), ("BTCUSDT.BINANCE", "BTCUSDT"), ("ETH-USDT", "ETHUSDT")])
def test_to_venue_symbol(raw, expected):
    assert LiveTradingManager.to_venue_symbol(raw) == expected


@pytest.mark.parametrize("raw", ["", "EUR/USD.SIM", "EUR/USD.XYZ", "A", "BTC USDT; DROP", "A.B.C"])
def test_to_venue_symbol_rejects_garbage(raw):
    with pytest.raises(ValueError):
        LiveTradingManager.to_venue_symbol(raw)


def _conn(status="connected"):
    return type("C", (), {"status": status, "binance_spot_api": object(), "bybit_account_api": object()})()


def test_resolve_venue_never_picks_silently_and_ignores_unverified():
    m = LiveTradingManager()
    with pytest.raises(RuntimeError, match="No verified"):
        m.resolve_venue()
    m._connections["binance"] = _conn("connected_offline")
    with pytest.raises(RuntimeError):
        m.resolve_venue()
    m._connections["binance"] = _conn()
    assert m.resolve_venue() == "BINANCE"
    m._connections["bybit"] = _conn()
    with pytest.raises(RuntimeError, match="specify the venue"):
        m.resolve_venue()
    assert m.resolve_venue("bybit") == "BYBIT"
    with pytest.raises(RuntimeError):
        m.resolve_venue("kraken")


async def test_offline_connection_can_reconcile_and_cancel_but_not_trade_and_recovers(mock_http):
    mgr = LiveTradingManager()
    client = _binance()
    mgr._connections["binance"] = type("C", (), {"status": "connected_offline", "binance_spot_api": client, "bybit_account_api": None})()
    assert mgr.ready_venues() == []
    with pytest.raises(RuntimeError):
        await mgr.send_order("BINANCE", symbol="BTCUSDT", side="BUY", order_type="MARKET", qty="1", price=None,
                             client_order_id="nw1")                          # trading needs a verified connection
    mock_http["handler"] = lambda req: httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."})
    assert await mgr.fetch_order("BINANCE", "BTCUSDT", "nw1") is None        # lookups work while unverified
    mock_http["handler"] = lambda req: httpx.Response(200, json={"canTrade": True, "balances": []})
    await mgr.reverify_offline()
    assert mgr.ready_venues() == ["BINANCE"]


async def test_free_balance_reads_only_unlocked_funds(mock_http):
    mgr = LiveTradingManager()
    mgr._connections["binance"] = type("C", (), {"status": "connected", "binance_spot_api": _binance(), "bybit_account_api": None})()
    mock_http["handler"] = lambda req: httpx.Response(200, json={"balances": [
        {"asset": "BTC", "free": "0.4", "locked": "0.1"}, {"asset": "ETH", "free": "2", "locked": "0"}]})
    assert await mgr.free_balance("BINANCE", "BTC") == Decimal("0.4")
    assert await mgr.free_balance("BINANCE", "DOGE") == Decimal("0")
