"""
Order lifecycle service against a scripted fake venue: paper vs live, persist-before-send,
outcome classification, idempotency, risk fail-closed, cancel, reconcile, kill switch.
"""

from decimal import Decimal

import asyncio

import pytest
import pytest_asyncio
from fastapi import HTTPException

import database
import order_service as osvc
import order_state as st
import order_store
import trading_control
from exchange_clients import (
    ExchangeHTTPError,
    ExchangeTransportError,
    SymbolRules,
    VenueOrder,
)
from risk_engine import RiskCheckError

pytestmark = pytest.mark.asyncio

RULES = SymbolRules("BINANCE", "BTCUSDT", tick_size=Decimal("0.01"), step_size=Decimal("0.001"),
                    min_qty=Decimal("0.001"), max_qty=Decimal("100"), min_notional=Decimal("10"))


class FakeManager:
    """Scriptable stand-in for LiveTradingManager (the exchange side)."""

    def __init__(self):
        self.connected = True
        self.sent = []                 # every send_order call
        self.cancels = []
        self.send_behavior = None      # callable(kwargs) -> dict, or raises
        self.fetch_behavior = None     # callable(client_order_id) -> VenueOrder | None, or raises
        self.cancel_behavior = None
        self.equity = Decimal("10000")
        self.db_status_at_send = None
        self.price = Decimal("30000")
        self.rules_gate = None          # asyncio.Event: freezes rules() until set
        self.send_gate = None           # asyncio.Event: freezes send_order() until set
        self.send_started = asyncio.Event()
        self.free = {"BTC": Decimal("0.5004")}
        self.venues = ["BINANCE"]
        self.equities = {}              # per-venue override

    # routing
    def is_connected(self, adapter_id=None):
        return self.connected

    def ready_venues(self):
        return list(self.venues) if self.connected else []

    def resolve_venue(self, requested=None):
        if not self.connected:
            raise RuntimeError("No verified exchange connection. Connect an adapter first.")
        if requested:
            if requested.upper() not in self.venues:
                raise RuntimeError(f"{requested} is not connected/verified")
            return requested.upper()
        if len(self.venues) > 1:
            raise RuntimeError("Several venues connected: specify the venue")
        return self.venues[0]

    async def free_balance(self, venue, asset):
        return self.free.get(asset, Decimal(0))

    @staticmethod
    def to_venue_symbol(instrument):
        from live_trading import LiveTradingManager
        return LiveTradingManager.to_venue_symbol(instrument)

    async def rules(self, venue, symbol):
        if self.rules_gate is not None:
            await self.rules_gate.wait()
        return RULES

    async def reference_price(self, venue, symbol):
        return self.price

    async def equity_usdt(self, venue):
        return self.equities.get(venue, self.equity)

    # orders
    async def send_order(self, venue, **kw):
        row = await order_store.get_by_client_id(kw["client_order_id"])
        self.db_status_at_send = row["status"] if row else None   # must already be persisted
        self.sent.append(kw)
        self.send_started.set()
        if self.send_gate is not None:
            await self.send_gate.wait()
        if self.send_behavior:
            return self.send_behavior(kw)
        return {"venue_order_id": "1001", "client_order_id": kw["client_order_id"], "raw_status": "NEW",
                "executed_qty": Decimal(0), "avg_price": None}

    async def fetch_order(self, venue, symbol, client_order_id):
        if self.fetch_behavior:
            return self.fetch_behavior(client_order_id)
        return None

    async def cancel_venue_order(self, venue, symbol, *, client_order_id, order_id=None):
        self.cancels.append(client_order_id)
        if self.cancel_behavior:
            return self.cancel_behavior(client_order_id)
        return {"status": "CANCELED"}


def vo(cid, raw="NEW", executed="0", oid="1001", avg=None):
    return VenueOrder("BINANCE", oid, cid, raw, Decimal(executed), Decimal(avg) if avg else None)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "svc.db")
    await database.init_db()
    monkeypatch.setattr(osvc, "LOOKUP_DELAY", 0)
    fake = FakeManager()
    service = osvc.OrderService(fake)
    # live-ready: explicit limits saved, live mode, armed
    await database.update_risk_limits({"max_position_size": 50_000, "max_daily_loss": 500, "max_orders_per_day": 20})
    await trading_control.set_mode("live", "admin")
    await trading_control.arm("admin")
    return service, fake


def order(**kw):
    base = {"instrument": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": Decimal("0.5"),
            "price": Decimal("30000"), "leverage": 1.0, "venue": None}
    base.update(kw)
    return base


async def place(service, key=None, user="alice", **kw):
    return await service.place(user=user, order=order(**kw), idempotency_key=key)


# ── paper vs live ────────────────────────────────────────────────────────────

async def test_paper_mode_never_touches_the_exchange_even_when_connected(env):
    service, fake = env
    await trading_control.set_mode("paper", "admin")
    res = await place(service, instrument="EUR/USD.SIM", type="MARKET", price=None)
    assert res["mode"] == "paper" and res["order"]["status"] == "PENDING"
    assert fake.sent == []


async def test_live_requires_mode_live_and_armed(env):
    service, fake = env
    await trading_control.disarm("admin", "test")
    with pytest.raises(HTTPException) as ei:
        await place(service)
    assert ei.value.status_code == 409 and ei.value.detail["error"] == "trading_disabled"
    assert fake.sent == []


async def test_live_never_falls_back_to_paper_when_venue_is_down(env):
    service, fake = env
    fake.connected = False
    with pytest.raises(HTTPException) as ei:
        await place(service)
    assert ei.value.status_code == 503 and ei.value.detail["error"] == "venue_unavailable"
    assert await order_store.list_open_live() == []
    assert (await database.list_orders()) == []        # nothing recorded as a "paper" order either


async def test_sim_instruments_are_refused_in_live_mode(env):
    service, _ = env
    with pytest.raises(HTTPException) as ei:
        await place(service, instrument="EUR/USD.SIM")
    assert ei.value.status_code == 400


# ── persist-before-send and outcomes ─────────────────────────────────────────

async def test_order_is_persisted_pending_submit_before_it_is_sent(env):
    service, fake = env
    res = await place(service)
    assert fake.db_status_at_send == st.PENDING_SUBMIT
    row = res["order"]
    assert row["status"] == st.SUBMITTED and row["exchange_order_id"] == "1001"
    assert fake.sent[0]["client_order_id"] == row["client_order_id"]
    assert fake.sent[0]["qty"] == "0.5" and fake.sent[0]["price"] == "30000"
    assert [e["event"] for e in await order_store.list_events(row["id"])] == ["created", "submitted"]


async def test_venue_rejection_marks_rejected_and_returns_422(env):
    service, fake = env

    def reject(kw):
        raise ExchangeHTTPError(400, "Account has insufficient balance", -2010, venue="BINANCE")

    fake.send_behavior = reject
    with pytest.raises(HTTPException) as ei:
        await place(service)
    assert ei.value.status_code == 422 and ei.value.detail["error"] == "exchange_rejected"
    row = await order_store.get_order(ei.value.detail["order_id"])
    assert row["status"] == st.REJECTED and "insufficient" in row["last_error"]


async def test_not_sent_marks_rejected_and_returns_503(env):
    service, fake = env

    def refused(kw):
        raise ExchangeTransportError("connection refused", maybe_sent=False)

    fake.send_behavior = refused
    with pytest.raises(HTTPException) as ei:
        await place(service)
    assert ei.value.status_code == 503 and ei.value.detail["error"] == "venue_unreachable"
    assert (await order_store.get_order(ei.value.detail["order_id"]))["status"] == st.REJECTED


async def test_timeout_is_unknown_then_resolved_by_client_id_without_resubmitting(env):
    service, fake = env

    def timeout(kw):
        raise ExchangeTransportError("read timeout", maybe_sent=True)

    fake.send_behavior = timeout
    fake.fetch_behavior = lambda cid: vo(cid, "NEW")             # the order DID reach the venue
    res = await place(service)
    assert res["reconciled"] is True and res["order"]["status"] == st.SUBMITTED
    assert len(fake.sent) == 1, "an unknown outcome must never be blindly retried"
    events = [e["event"] for e in await order_store.list_events(res["order"]["id"])]
    assert "outcome_unknown" in events and "reconciled" in events


async def test_timeout_and_order_filled_on_venue_is_recorded_as_filled(env):
    service, fake = env
    fake.send_behavior = lambda kw: (_ for _ in ()).throw(ExchangeHTTPError(503, "unavailable", None, venue="BINANCE"))
    fake.fetch_behavior = lambda cid: vo(cid, "FILLED", executed="0.5", avg="30000")
    res = await place(service)
    assert res["order"]["status"] == st.FILLED and res["order"]["filled_qty"] == pytest.approx(0.5)
    assert res["order"]["avg_price"] == pytest.approx(30000)


async def test_unknown_outcome_that_cannot_be_resolved_returns_202_and_stays_unknown(env):
    service, fake = env
    fake.send_behavior = lambda kw: (_ for _ in ()).throw(ExchangeTransportError("read timeout", maybe_sent=True))
    fake.fetch_behavior = lambda cid: None                      # venue does not (yet) know the order
    with pytest.raises(HTTPException) as ei:
        await place(service)
    assert ei.value.status_code == 202 and ei.value.detail["error"] == "outcome_unknown"
    assert "Do NOT resubmit" in ei.value.detail["message"]
    assert ei.value.detail["order"]["status"] == st.UNKNOWN


async def test_unexpected_exception_after_persist_is_treated_as_unknown_not_lost(env):
    service, fake = env

    def bug(kw):
        raise ZeroDivisionError("bug in our code")

    fake.send_behavior = bug
    fake.fetch_behavior = lambda cid: None
    with pytest.raises(HTTPException) as ei:
        await place(service)
    assert ei.value.status_code == 202
    assert (await order_store.get_order(ei.value.detail["order"]["id"]))["status"] == st.UNKNOWN


# ── reconciler ───────────────────────────────────────────────────────────────

async def _unknown_order(service, fake):
    fake.send_behavior = lambda kw: (_ for _ in ()).throw(ExchangeTransportError("read timeout", maybe_sent=True))
    fake.fetch_behavior = lambda cid: None
    with pytest.raises(HTTPException) as ei:
        await place(service)
    fake.send_behavior = None
    return ei.value.detail["order"]


async def test_reconciler_declares_never_reached_only_after_grace_and_repeated_checks(env, monkeypatch):
    service, fake = env
    o = await _unknown_order(service, fake)
    monkeypatch.setattr(osvc, "NOT_FOUND_GRACE_SECS", 0)
    # LOOKUP_ATTEMPTS lookups already counted as 'not found'; keep reconciling
    for _ in range(osvc.NOT_FOUND_MIN_CHECKS):
        await service.reconcile_all()
    final = await order_store.get_order(o["id"])
    assert final["status"] == st.REJECTED and "not found" in final["last_error"]


async def test_reconciler_does_not_reject_within_grace_period(env):
    service, fake = env
    o = await _unknown_order(service, fake)
    for _ in range(6):
        await service.reconcile_all()
    assert (await order_store.get_order(o["id"]))["status"] == st.UNKNOWN     # default grace is 120s


async def test_reconciler_follows_partial_then_full_fill_and_never_regresses(env):
    service, fake = env
    res = await place(service)
    cid, oid = res["order"]["client_order_id"], res["order"]["id"]
    fake.fetch_behavior = lambda c: vo(c, "PARTIALLY_FILLED", executed="0.2", avg="30000")
    await service.reconcile_all()
    assert (await order_store.get_order(oid))["status"] == st.PARTIALLY_FILLED
    fake.fetch_behavior = lambda c: vo(c, "FILLED", executed="0.5", avg="30001")
    await service.reconcile_all()
    row = await order_store.get_order(oid)
    assert row["status"] == st.FILLED and row["filled_qty"] == pytest.approx(0.5)
    fake.fetch_behavior = lambda c: vo(c, "NEW")                        # stale/odd venue answer
    await service.reconcile_order(row)
    assert (await order_store.get_order(oid))["status"] == st.FILLED
    assert await order_store.list_open_live() == []


async def test_reconciler_ignores_unrecognised_venue_status(env):
    service, fake = env
    res = await place(service)
    fake.fetch_behavior = lambda c: vo(c, "SOME_FUTURE_STATUS")
    await service.reconcile_all()
    assert (await order_store.get_order(res["order"]["id"]))["status"] == st.SUBMITTED


async def test_reconciler_survives_venue_errors(env):
    service, fake = env
    res = await place(service)

    def boom(c):
        raise ExchangeTransportError("timeout", maybe_sent=True)

    fake.fetch_behavior = boom
    assert await service.reconcile_all() == 1
    assert (await order_store.get_order(res["order"]["id"]))["status"] == st.SUBMITTED


# ── idempotency ──────────────────────────────────────────────────────────────

async def test_same_idempotency_key_returns_same_order_and_sends_once(env):
    service, fake = env
    a = await place(service, key="key-12345678")
    b = await place(service, key="key-12345678")
    assert b["idempotent_replay"] is True and b["order"]["id"] == a["order"]["id"]
    assert len(fake.sent) == 1


async def test_idempotency_key_reused_with_a_different_order_is_a_conflict(env):
    service, fake = env
    await place(service, key="key-12345678")
    with pytest.raises(HTTPException) as ei:
        await place(service, key="key-12345678", quantity=Decimal("1"))
    assert ei.value.status_code == 409 and ei.value.detail["error"] == "idempotency_conflict"
    assert len(fake.sent) == 1


async def test_idempotency_keys_are_scoped_per_user(env):
    service, fake = env
    await place(service, key="key-12345678", user="alice")
    await place(service, key="key-12345678", user="bob")
    assert len(fake.sent) == 2


# ── pre-trade validation and risk (fail-closed) ──────────────────────────────

async def test_invalid_lot_size_is_refused_before_anything_is_persisted_or_sent(env):
    service, fake = env
    with pytest.raises(HTTPException) as ei:
        await place(service, quantity=Decimal("0.0015"))
    assert ei.value.status_code == 422 and ei.value.detail["error"] == "invalid_order"
    assert fake.sent == [] and await order_store.list_open_live() == []


async def test_stop_orders_are_not_supported_live(env):
    service, _ = env
    with pytest.raises(HTTPException) as ei:
        await place(service, type="STOP")
    assert ei.value.status_code == 422


async def test_risk_rejects_order_value_over_limit(env):
    service, fake = env
    await database.update_risk_limits({"max_position_size": 1000})
    with pytest.raises(RiskCheckError):
        await place(service)                                           # 0.5 * 30000 = 15000 > 1000
    assert fake.sent == []


async def test_market_order_is_valued_with_the_reference_price(env):
    service, fake = env
    await database.update_risk_limits({"max_position_size": 1000})
    with pytest.raises(RiskCheckError):
        await place(service, type="MARKET", price=None)                # 0.5 * ref 30000 = 15000 > 1000
    assert fake.sent == []


async def test_live_requires_limits_to_have_been_saved_by_an_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "fresh.db")
    await database.init_db()                                          # only seeded defaults
    fake = FakeManager()
    service = osvc.OrderService(fake)
    await trading_control.set_mode("live", "admin")
    await trading_control.arm("admin")
    with pytest.raises(RiskCheckError, match="not saved by an admin"):
        await place(service)
    assert fake.sent == []


async def test_daily_order_limit(env):
    service, fake = env
    await database.update_risk_limits({"max_orders_per_day": 2})
    await place(service)
    await place(service)
    with pytest.raises(RiskCheckError, match="Daily order limit"):
        await place(service)
    assert len(fake.sent) == 2


async def test_daily_loss_breach_trips_the_kill_switch(env):
    service, fake = env
    await place(service)                                              # records day-start equity 10000
    fake.equity = Decimal("9400")                                      # lost 600 > limit 500
    with pytest.raises(RiskCheckError, match="Trading halted"):
        await place(service)
    assert (await trading_control.get_state())["trading_enabled"] is False
    with pytest.raises(HTTPException) as ei:
        await place(service)
    assert ei.value.detail["error"] == "trading_disabled"


async def test_unreadable_equity_refuses_the_order(env):
    service, fake = env
    fake.equity = None
    with pytest.raises(RiskCheckError, match="equity could not be read"):
        await place(service)
    assert fake.sent == []


async def test_max_open_exposure_counts_resting_orders(env):
    service, fake = env
    await database.update_risk_limits({"max_open_exposure": 20_000})
    await place(service)                                              # 15000 resting
    with pytest.raises(RiskCheckError, match="max_open_exposure"):
        await place(service)                                          # +15000 -> 30000 > 20000


async def test_spot_leverage_above_one_is_refused(env):
    service, _ = env
    with pytest.raises(RiskCheckError, match="leverage"):
        await place(service, leverage=5.0)


# ── cancel ───────────────────────────────────────────────────────────────────

async def test_cancel_confirms_final_state_with_the_venue(env):
    service, fake = env
    res = await place(service)
    fake.fetch_behavior = lambda c: vo(c, "CANCELED")
    out = await service.cancel(user="alice", order_id=res["order"]["id"])
    assert out["order"]["status"] == st.CANCELLED and fake.cancels == [res["order"]["client_order_id"]] \
        or out["order"]["status"] == st.CANCELLED


async def test_cancel_does_not_assume_success_if_venue_still_shows_it_open(env):
    service, fake = env
    res = await place(service)
    fake.fetch_behavior = lambda c: vo(c, "NEW")                       # cancel ack but order still live
    out = await service.cancel(user="alice", order_id=res["order"]["id"])
    assert out["order"]["status"] == st.SUBMITTED


async def test_cancel_with_unknown_outcome_is_202_and_status_unchanged(env):
    service, fake = env
    res = await place(service)

    def timeout(cid):
        raise ExchangeTransportError("timeout", maybe_sent=True)

    fake.cancel_behavior = timeout
    with pytest.raises(HTTPException) as ei:
        await service.cancel(user="alice", order_id=res["order"]["id"])
    assert ei.value.status_code == 202
    assert (await order_store.get_order(res["order"]["id"]))["status"] == st.SUBMITTED


async def test_cancel_of_already_filled_order_learns_real_state(env):
    service, fake = env
    res = await place(service)

    def too_late(cid):
        raise ExchangeHTTPError(400, "Unknown order sent.", -2011, venue="BINANCE")

    fake.cancel_behavior = too_late
    fake.fetch_behavior = lambda c: vo(c, "FILLED", executed="0.5")
    out = await service.cancel(user="alice", order_id=res["order"]["id"])
    assert out["order"]["status"] == st.FILLED


async def test_cancel_of_terminal_order_conflicts(env):
    service, fake = env
    res = await place(service)
    fake.fetch_behavior = lambda c: vo(c, "FILLED", executed="0.5")
    await service.reconcile_all()
    with pytest.raises(HTTPException) as ei:
        await service.cancel(user="alice", order_id=res["order"]["id"])
    assert ei.value.status_code == 409


async def test_cancel_of_unknown_order_that_venue_cannot_confirm_is_refused(env):
    service, fake = env
    o = await _unknown_order(service, fake)
    with pytest.raises(HTTPException) as ei:
        await service.cancel(user="alice", order_id=o["id"])
    assert ei.value.status_code == 409 and ei.value.detail["error"] == "outcome_unknown"
    assert fake.cancels == []


async def test_cancel_unknown_order_id_is_404(env):
    service, _ = env
    with pytest.raises(HTTPException) as ei:
        await service.cancel(user="alice", order_id="ORD-NOPE")
    assert ei.value.status_code == 404


# ── kill switch / restart ────────────────────────────────────────────────────

async def test_kill_switch_blocks_new_orders_and_cancels_open_ones(env):
    service, fake = env
    a = await place(service)
    b = await place(service)
    fake.fetch_behavior = lambda c: vo(c, "CANCELED")
    out = await service.kill(user="alice", reason="panic")
    assert out["state"]["trading_enabled"] is False and out["state"]["reason"] == "panic"
    assert len(out["cancel_results"]) == 2 and out["all_clear"] is True and out["unresolved_orders"] == 0
    assert {(await order_store.get_order(x["order"]["id"]))["status"] for x in (a, b)} == {st.CANCELLED}
    with pytest.raises(HTTPException) as ei:
        await place(service)
    assert ei.value.status_code == 409


async def test_restart_always_comes_up_disarmed(env):
    service, _ = env
    assert (await trading_control.get_state())["trading_enabled"] is True
    state = await trading_control.reset_on_startup()
    assert state["trading_enabled"] is False and state["mode"] == "live"
    assert "restart" in state["reason"]


async def test_arming_requires_live_mode_and_mode_change_disarms(env):
    await trading_control.set_mode("paper", "admin")
    with pytest.raises(trading_control.TradingDisabled):
        await trading_control.arm("admin")
    await trading_control.set_mode("live", "admin")
    await trading_control.arm("admin")
    assert (await trading_control.set_mode("live", "admin"))["trading_enabled"] is False


# ═════════════════════════════════════════════════════════════════════════════
# Findings from the adversarial review (each test pins one confirmed defect)
# ═════════════════════════════════════════════════════════════════════════════

async def test_kill_pulled_while_order_waits_for_venue_data_blocks_the_send(env):
    """Kill during rules()/reference_price(): the order must not be sent afterwards."""
    service, fake = env
    fake.rules_gate = asyncio.Event()
    task = asyncio.create_task(place(service))
    await asyncio.sleep(0.05)                       # handler is now blocked in rules()
    await service.kill(user="alice", reason="panic")
    fake.rules_gate.set()
    with pytest.raises(HTTPException) as ei:
        await task
    assert ei.value.status_code == 409 and ei.value.detail["error"] == "trading_disabled"
    assert fake.sent == []


async def test_kill_pulled_while_the_request_is_in_flight_cancels_the_order_afterwards(env):
    service, fake = env
    fake.send_gate = asyncio.Event()
    task = asyncio.create_task(place(service))
    await asyncio.wait_for(fake.send_started.wait(), 2)   # request is "on the wire"
    await service.kill(user="alice", reason="panic")      # order unknown to the DB view as SUBMITTED yet
    fake.fetch_behavior = lambda c: vo(c, "CANCELED")
    fake.send_gate.set()
    res = await task
    assert res["order"]["status"] == st.CANCELLED         # taken back right after the venue accepted it
    assert fake.cancels == [res["order"]["client_order_id"]]


async def test_kill_cannot_cancel_an_unknown_order_yet_but_reconciler_cancels_it_when_it_appears(env):
    service, fake = env
    o = await _unknown_order(service, fake)               # UNKNOWN, venue has no record
    out = await service.kill(user="alice", reason="panic")
    assert out["all_clear"] is False and out["unresolved_orders"] == 1
    assert (await trading_control.get_state())["cancel_pending"] is True
    assert fake.cancels == []
    # the venue finally shows the order as live -> the next reconcile round must cancel it
    fake.fetch_behavior = lambda c: vo(c, "NEW")
    await service.reconcile_all()
    assert fake.cancels == [o["client_order_id"]]


async def test_rearming_clears_the_pending_cancel_intent(env):
    service, fake = env
    await service.kill(user="alice", reason="panic")
    assert (await trading_control.get_state())["cancel_pending"] is True
    await trading_control.arm("admin")
    assert (await trading_control.get_state())["cancel_pending"] is False


async def test_client_disconnect_mid_request_does_not_abandon_a_persisted_order(env):
    """FastAPI cancels the handler when the client goes away: the persisted order must still be sent."""
    service, fake = env
    fake.send_gate = asyncio.Event()
    task = asyncio.create_task(place(service))
    await asyncio.wait_for(fake.send_started.wait(), 2)
    task.cancel()                                          # client disconnected
    with pytest.raises(asyncio.CancelledError):
        await task
    fake.send_gate.set()
    await asyncio.sleep(0.1)
    rows = await order_store.list_open_live()
    assert len(rows) == 1 and rows[0]["status"] == st.SUBMITTED and rows[0]["exchange_order_id"] == "1001"


async def test_instrument_venue_suffix_is_honoured_and_conflicts_are_refused(env):
    service, fake = env
    fake.venues = ["BINANCE", "BYBIT"]
    with pytest.raises(HTTPException) as ei:                # two venues, no hint: refuse to guess
        await place(service)
    assert ei.value.status_code == 503
    res = await place(service, instrument="BTCUSDT.BYBIT")
    assert res["order"]["venue"] == "BYBIT"
    with pytest.raises(HTTPException) as ei:
        await place(service, instrument="BTCUSDT.BINANCE", venue="BYBIT")
    assert ei.value.status_code == 400 and ei.value.detail["error"] == "venue_mismatch"


async def test_pairs_not_quoted_in_a_usd_like_asset_are_refused(env):
    service, fake = env
    with pytest.raises(HTTPException) as ei:
        await place(service, instrument="ETHBTC")
    assert ei.value.status_code == 422 and ei.value.detail["error"] == "unsupported_quote"
    assert fake.sent == []


async def test_zero_or_missing_reference_price_never_values_a_market_order_at_zero(env):
    service, fake = env
    fake.price = Decimal("0")
    with pytest.raises(HTTPException) as ei:
        await place(service, type="MARKET", price=None)
    assert ei.value.status_code == 503 and fake.sent == []


async def test_daily_loss_baseline_is_kept_per_venue(env):
    service, fake = env
    fake.venues = ["BINANCE", "BYBIT"]
    fake.equities = {"BINANCE": Decimal("10000"), "BYBIT": Decimal("100")}
    await place(service, venue="BINANCE")                   # baseline BINANCE=10000
    await place(service, venue="BYBIT", quantity=Decimal("0.001"))    # baseline BYBIT=100, no false trip
    assert (await trading_control.get_state())["trading_enabled"] is True
    fake.equities["BINANCE"] = Decimal("9400")               # real loss of 600 on BINANCE only
    with pytest.raises(RiskCheckError, match="Trading halted"):
        await place(service, venue="BINANCE")
    state = await trading_control.get_state()
    assert state["trading_enabled"] is False and state["cancel_pending"] is True


async def test_daily_loss_breach_takes_back_resting_orders_via_the_reconciler(env):
    service, fake = env
    first = await place(service)
    fake.equity = Decimal("9400")
    with pytest.raises(RiskCheckError):
        await place(service)
    fake.fetch_behavior = lambda c: vo(c, "NEW")
    await service.reconcile_all()
    assert fake.cancels == [first["order"]["client_order_id"]]


async def test_reconciler_reverifies_offline_venues(env):
    service, fake = env
    calls = []

    async def reverify():
        calls.append(1)

    fake.reverify_offline = reverify
    await service.reconcile_all()
    assert calls == [1]


async def test_kill_reports_filled_orders_as_filled_not_cancelled(env):
    service, fake = env
    res = await place(service)

    def too_late(cid):
        raise ExchangeHTTPError(400, "Unknown order sent.", -2011, venue="BINANCE")

    fake.cancel_behavior = too_late
    fake.fetch_behavior = lambda c: vo(c, "FILLED", executed="0.5")
    out = await service.kill(user="alice", reason="panic")
    assert out["cancel_results"][0]["status"] == st.FILLED and out["cancel_results"][0]["result"] == "closed"
