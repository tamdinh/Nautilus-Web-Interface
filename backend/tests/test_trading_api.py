"""Trading control API (mode / arm / kill), step-up TOTP, and the live order endpoints end to end."""

import asyncio
from decimal import Decimal

import pyotp
import pytest

import database

RISK = {"max_position_size": 50000, "max_daily_loss": 500, "max_orders_per_day": 20}


def _enable_2fa(client):
    secret = client.get("/api/auth/2fa/setup").json()["secret"]
    totp = pyotp.TOTP(secret)
    assert client.post("/api/auth/2fa/enable", json={"totp_code": totp.now()}).status_code == 200
    return totp


@pytest.fixture
def trader_headers(client):
    client.post("/api/users", json={"username": "trader7", "password": "secret1234", "role": "trader"})
    r = client.post("/api/auth/login", json={"username": "trader7", "password": "secret1234"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _go_live(client):
    client.post("/api/risk/limits", json=RISK)
    assert client.post("/api/trading/mode", json={"mode": "live"}).status_code == 200
    assert client.post("/api/trading/arm", json={}).status_code == 200


LIVE_ORDER = {"instrument": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": "0.5", "price": "30000"}
_N = {"n": 0}


def key():
    _N["n"] += 1
    return {"Idempotency-Key": f"test-key-{_N['n']:06d}"}


def test_default_state_is_paper_and_disarmed_with_reasons(client):
    s = client.get("/api/trading/state").json()
    assert s["mode"] == "paper" and s["trading_enabled"] is False and s["ready_for_live"] is False
    assert any("adapter" in p for p in s["problems"])
    assert any("never been reviewed" in p for p in s["problems"])


def test_cannot_switch_to_live_until_ready(client):
    r = client.post("/api/trading/mode", json={"mode": "live"})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "not_ready_for_live"
    assert client.get("/api/trading/state").json()["mode"] == "paper"


def test_full_enable_flow_and_kill(client, fake_venue):
    client.post("/api/risk/limits", json=RISK)
    assert client.post("/api/trading/arm", json={}).status_code == 409           # not in live mode yet
    r = client.post("/api/trading/mode", json={"mode": "live", "reason": "go"})
    assert r.status_code == 200 and r.json()["mode"] == "live" and r.json()["trading_enabled"] is False
    r = client.post("/api/trading/arm", json={})
    assert r.status_code == 200 and r.json()["trading_enabled"] is True
    r = client.post("/api/trading/kill", json={"reason": "test", "cancel_open": False})
    assert r.status_code == 200 and r.json()["state"]["trading_enabled"] is False


def test_only_admin_can_change_mode_or_arm_but_any_trader_can_kill(client, fake_venue, trader_headers):
    assert client.post("/api/trading/mode", json={"mode": "live"}, headers=trader_headers).status_code == 403
    assert client.post("/api/trading/arm", json={}, headers=trader_headers).status_code == 403
    r = client.post("/api/trading/kill", json={"cancel_open": False}, headers=trader_headers)
    assert r.status_code == 200 and r.json()["state"]["trading_enabled"] is False


def test_step_up_blocks_live_without_2fa_and_rejects_replays(client, fake_venue, monkeypatch):
    monkeypatch.setenv("STEP_UP_REQUIRED", "1")
    client.post("/api/risk/limits", json=RISK)

    r = client.post("/api/trading/mode", json={"mode": "live"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "2fa_not_enabled"

    totp = _enable_2fa(client)
    r = client.post("/api/trading/mode", json={"mode": "live"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "step_up_required"
    r = client.post("/api/trading/mode", json={"mode": "live", "totp_code": "000000"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "invalid_totp"

    code = totp.now()
    assert client.post("/api/trading/mode", json={"mode": "live", "totp_code": code}).status_code == 200
    # the same code must not authorise a second dangerous action
    r = client.post("/api/trading/arm", json={"totp_code": code})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "totp_replayed"


def test_leaving_live_does_not_need_step_up(client, fake_venue, monkeypatch):
    client.post("/api/risk/limits", json=RISK)
    client.post("/api/trading/mode", json={"mode": "live"})
    monkeypatch.setenv("STEP_UP_REQUIRED", "1")
    r = client.post("/api/trading/mode", json={"mode": "paper"})
    assert r.status_code == 200 and r.json()["mode"] == "paper"


def test_risk_limits_are_validated(client):
    for bad in ({"max_daily_loss": -5}, {"max_leverage": "lots"}, {"max_orders_per_day": 1.5}, {"max_position_size": 1e99}):
        assert client.post("/api/risk/limits", json=bad).status_code == 422, bad
    r = client.post("/api/risk/limits", json={"max_daily_loss": 123, "unknown_key": "ignored"})
    assert r.status_code == 200 and r.json()["limits"]["max_daily_loss"] == 123
    assert "unknown_key" not in r.json()["limits"]


def test_live_order_endpoint_idempotency_events_and_cancel(client, fake_venue):
    from tests.test_order_service import vo

    _go_live(client)
    hdr = {"Idempotency-Key": "client-key-0001"}
    r1 = client.post("/api/orders", json=LIVE_ORDER, headers=hdr)
    assert r1.status_code == 200 and r1.json()["order"]["status"] == "SUBMITTED"
    r2 = client.post("/api/orders", json=LIVE_ORDER, headers=hdr)
    assert r2.json()["idempotent_replay"] is True and r2.json()["order"]["id"] == r1.json()["order"]["id"]
    assert len(fake_venue.sent) == 1

    oid = r1.json()["order"]["id"]
    events = client.get(f"/api/orders/{oid}/events").json()["events"]
    assert [e["event"] for e in events][:2] == ["created", "submitted"]

    fake_venue.fetch_behavior = lambda c: vo(c, "CANCELED")
    r = client.delete(f"/api/orders/{oid}")
    assert r.status_code == 200 and r.json()["order"]["status"] == "CANCELLED"
    assert client.delete(f"/api/orders/{oid}").status_code == 409            # already closed


def test_paper_by_default_then_refused_when_disarmed_and_malformed_key_rejected(client, fake_venue):
    r = client.post("/api/orders", json=LIVE_ORDER)
    assert r.status_code == 200 and r.json()["mode"] == "paper"              # default paper: recorded only
    assert fake_venue.sent == []
    assert client.post("/api/orders", json=LIVE_ORDER, headers={"Idempotency-Key": "short"}).status_code == 422
    _go_live(client)
    client.post("/api/trading/kill", json={"cancel_open": False})
    r = client.post("/api/orders", json=LIVE_ORDER, headers=key())
    assert r.status_code == 409 and r.json()["detail"]["error"] == "trading_disabled"


def test_order_quantity_is_exact_decimal_not_float(client, fake_venue):
    _go_live(client)
    body = {**LIVE_ORDER, "quantity": "0.100", "price": "29999.90"}
    r = client.post("/api/orders", json=body, headers=key())
    assert r.status_code == 200
    assert fake_venue.sent[0]["qty"] == "0.100" and fake_venue.sent[0]["price"] == "29999.90"


def test_live_close_position_goes_through_the_order_path(client, fake_venue):
    _go_live(client)
    fake_venue.free = {"BTC": Decimal("0.5004")}
    asyncio.run(database.replace_live_positions(
        "BINANCE", [{"instrument": "BTCUSDT", "side": "LONG", "quantity": 0.9}]))
    pos = [p for p in client.get("/api/positions").json() if p["instrument"] == "BTCUSDT"]
    assert pos
    r = client.post(f"/api/positions/{pos[0]['id']}/close")
    assert r.status_code == 200 and r.json()["closed_in_db"] is False
    sent = fake_venue.sent[0]
    assert sent["side"] == "SELL" and sent["order_type"] == "MARKET" and sent["qty"] == "0.500"   # floored to step 0.001
    assert client.post("/api/positions/NOPE/close").status_code == 404


# ── findings from the adversarial review ─────────────────────────────────────

def test_live_orders_require_an_idempotency_key(client, fake_venue):
    _go_live(client)
    r = client.post("/api/orders", json=LIVE_ORDER)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "idempotency_key_required"
    assert fake_venue.sent == []


def test_order_responses_and_lists_hide_internal_fields(client, fake_venue):
    _go_live(client)
    r = client.post("/api/orders", json=LIVE_ORDER, headers=key())
    order = r.json()["order"]
    assert "request_hash" not in order and "user_id" not in order
    listed = client.get("/api/orders").json()["orders"]
    assert all("request_hash" not in o and "user_id" not in o for o in listed)


def test_2fa_setup_cannot_replace_an_enabled_authenticator(client):
    totp = _enable_2fa(client)
    r = client.get("/api/auth/2fa/setup")
    assert r.status_code == 409
    # the original authenticator still works for step-up/login
    import database
    assert asyncio.run(database.get_user_2fa("admin"))["totp_secret"] == totp.secret


def test_risk_limit_changes_need_step_up_while_live_and_a_non_empty_body(client, fake_venue, monkeypatch):
    assert client.post("/api/risk/limits", json={}).status_code == 422
    client.post("/api/risk/limits", json=RISK)
    client.post("/api/trading/mode", json={"mode": "live"})
    monkeypatch.setenv("STEP_UP_REQUIRED", "1")
    r = client.post("/api/risk/limits", json={"max_daily_loss": 9_999_999})
    assert r.status_code == 403 and r.json()["detail"]["error"] in ("2fa_not_enabled", "step_up_required")
    monkeypatch.setenv("STEP_UP_REQUIRED", "0")
    assert client.post("/api/risk/limits", json={"max_daily_loss": 400}).status_code == 200


def test_kill_switch_is_never_rate_limited(client, fake_venue, monkeypatch):
    import nautilus_fastapi
    monkeypatch.setattr(nautilus_fastapi, "_GLOBAL_RATE_LIMIT", 1)
    for _ in range(5):
        assert client.post("/api/trading/kill", json={"cancel_open": False}).status_code == 200


def test_disconnect_is_refused_while_live_orders_are_open_and_credentials_survive(client, fake_venue):
    import database
    fake_venue.venues = ["BINANCE"]
    _go_live(client)
    placed = client.post("/api/orders", json=LIVE_ORDER, headers=key()).json()["order"]
    asyncio.run(database.upsert_adapter_config("binance", "connected", api_key="enc-k", api_secret="enc-s"))
    r = client.post("/api/adapters/binance/disconnect")
    assert r.status_code == 409 and r.json()["detail"]["error"] == "open_orders"
    assert placed["id"] in r.json()["detail"]["order_ids"]
    # a status-only update must not wipe the stored credentials
    asyncio.run(database.upsert_adapter_config("binance", "disconnected"))
    cfg = asyncio.run(database.get_adapter_config("binance"))
    assert cfg["api_key"] == "enc-k" and cfg["api_secret"] == "enc-s"


def test_clean_never_deletes_live_orders(client, fake_venue):
    import order_store
    _go_live(client)
    placed = client.post("/api/orders", json=LIVE_ORDER, headers=key()).json()["order"]

    async def age_it():
        async with database.connect() as db:
            await db.execute("UPDATE orders SET status='CANCELLED', timestamp='2020-01-01T00:00:00+00:00' WHERE id=?",
                             (placed["id"],))
            await db.commit()

    asyncio.run(age_it())
    assert client.post("/api/database/clean", json={}).status_code == 200
    assert asyncio.run(order_store.get_order(placed["id"])) is not None


def test_restore_requires_step_up_and_ends_disarmed(client, fake_venue, monkeypatch):
    _go_live(client)
    backup = client.post("/api/database/backup", json={}).json()
    assert backup["success"] is True
    name = backup["backup_file"].replace("\\", "/").split("/")[-1]
    monkeypatch.setenv("STEP_UP_REQUIRED", "1")
    r = client.post("/api/database/restore", json={"backup_file": name})
    assert r.status_code == 403
    monkeypatch.setenv("STEP_UP_REQUIRED", "0")
    r = client.post("/api/database/restore", json={"backup_file": name})
    assert r.status_code == 200
    assert client.get("/api/trading/state").json()["trading_enabled"] is False   # restored "armed" state is not trusted
