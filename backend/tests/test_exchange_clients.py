"""
Tests for the httpx-based Binance/Bybit REST clients and LiveTradingManager error mapping.
All HTTP traffic is served by httpx.MockTransport — nothing reaches a real exchange.
"""

import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = pytest.mark.asyncio

import exchange_clients
from exchange_clients import BinanceRestClient, BybitRestClient, ExchangeHTTPError
from live_trading import BinanceAuthError, BybitAuthError, LiveTradingManager


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


# ── Binance ──────────────────────────────────────────────────────────────────

async def test_binance_account_info_is_signed(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"canTrade": True, "balances": []})
    client = BinanceRestClient("key12345", "secret12345", base_url="https://binance.test")

    info = await client.account_info()

    assert info["canTrade"] is True
    req = mock_http["requests"][0]
    assert req.headers["X-MBX-APIKEY"] == "key12345"
    query = urlparse(str(req.url)).query
    unsigned, _, sig = query.rpartition("&signature=")
    assert sig == hmac.new(b"secret12345", unsigned.encode(), hashlib.sha256).hexdigest()
    assert "timestamp=" in unsigned


async def test_binance_401_maps_to_auth_error(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key"})
    manager = LiveTradingManager()
    api = manager._make_binance_spot_api("key12345", "secret12345")
    with pytest.raises(BinanceAuthError):
        await manager._verify_binance_credentials("key12345", "secret12345", spot_api=api)


async def test_binance_invalid_json_is_connection_error(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, content=b"<html>not json</html>")
    manager = LiveTradingManager()
    api = manager._make_binance_spot_api("key12345", "secret12345")
    with pytest.raises(ConnectionError):
        await manager._verify_binance_credentials("key12345", "secret12345", spot_api=api)


async def test_binance_network_failure_is_connection_error(mock_http):
    def boom(req):
        raise httpx.ConnectError("no route")

    mock_http["handler"] = boom
    manager = LiveTradingManager()
    api = manager._make_binance_spot_api("key12345", "secret12345")
    with pytest.raises(ConnectionError):
        await manager._verify_binance_credentials("key12345", "secret12345", spot_api=api)


async def test_binance_submit_limit_order_payload(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"orderId": 42, "status": "NEW", "clientOrderId": "abc"})
    manager = LiveTradingManager()
    api = BinanceRestClient("key12345", "secret12345", base_url="https://binance.test")
    conn = type("C", (), {"binance_spot_api": api})()

    result = await manager._submit_binance_order(
        conn, {"instrument": "BTC/USDT", "side": "buy", "type": "limit", "quantity": 0.5, "price": 100}
    )

    assert result["order_id"] == "42" and result["exchange"] == "BINANCE"
    req = mock_http["requests"][0]
    assert req.method == "POST"
    q = parse_qs(urlparse(str(req.url)).query)
    assert q["symbol"] == ["BTCUSDT"] and q["side"] == ["BUY"] and q["type"] == ["LIMIT"]
    assert q["timeInForce"] == ["GTC"] and q["price"] == ["100"] and q["quantity"] == ["0.5"]


async def test_binance_limit_order_requires_price():
    manager = LiveTradingManager()
    conn = type("C", (), {"binance_spot_api": object()})()
    with pytest.raises(ValueError):
        await manager._submit_binance_order(conn, {"instrument": "BTCUSDT", "type": "LIMIT", "quantity": 1})


# ── Bybit ────────────────────────────────────────────────────────────────────

async def test_bybit_get_is_signed(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 0, "result": {"unifiedMarginStatus": 4}})
    client = BybitRestClient("key12345", "secret12345", base_url="https://bybit.test")

    info = await client.account_info()

    assert info["unifiedMarginStatus"] == 4
    req = mock_http["requests"][0]
    ts = req.headers["X-BAPI-TIMESTAMP"]
    expected = hmac.new(b"secret12345", f"{ts}key12345{5000}".encode(), hashlib.sha256).hexdigest()
    assert req.headers["X-BAPI-SIGN"] == expected
    assert req.headers["X-BAPI-API-KEY"] == "key12345"


async def test_bybit_post_signs_raw_body(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 0, "result": {"orderId": "9"}})
    client = BybitRestClient("key12345", "secret12345", base_url="https://bybit.test")

    await client.place_order("BTCUSDT", "Buy", "Limit", "1", price="100")

    req = mock_http["requests"][0]
    body = req.content.decode()
    assert json.loads(body)["timeInForce"] == "GTC"
    ts = req.headers["X-BAPI-TIMESTAMP"]
    expected = hmac.new(b"secret12345", f"{ts}key12345{5000}{body}".encode(), hashlib.sha256).hexdigest()
    assert req.headers["X-BAPI-SIGN"] == expected


async def test_bybit_retcode_auth_error_on_http_200(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 10003, "retMsg": "API key is invalid"})
    manager = LiveTradingManager()
    manager._make_bybit_account_api = lambda k, s: BybitRestClient(k, s, base_url="https://bybit.test")
    with pytest.raises(BybitAuthError):
        await manager.connect_bybit("key12345", "secret12345")


async def test_bybit_non_auth_error_connects_offline(mock_http):
    mock_http["handler"] = lambda req: httpx.Response(200, json={"retCode": 10016, "retMsg": "server error"})
    manager = LiveTradingManager()
    manager._make_bybit_account_api = lambda k, s: BybitRestClient(k, s, base_url="https://bybit.test")
    result = await manager.connect_bybit("key12345", "secret12345")
    assert result["verified"] is False
    assert manager.is_connected("bybit")


async def test_exchange_http_error_carries_status_and_code():
    err = ExchangeHTTPError(403, "forbidden", -2015)
    assert err.status == 403 and err.code == -2015
