"""Order lifecycle state machine and persistence (compare-and-set, idempotency, events)."""

from decimal import Decimal

import pytest
import pytest_asyncio

import database
import order_state as st
import order_store


@pytest_asyncio.fixture
async def store(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "t.db")
    await database.init_db()
    return order_store



def test_transitions_allowed_and_forbidden():
    assert st.can_transition(st.PENDING_SUBMIT, st.SUBMITTED)
    assert st.can_transition(st.UNKNOWN, st.FILLED)
    assert st.can_transition(st.SUBMITTED, st.SUBMITTED)  # idempotent
    assert not st.can_transition(st.FILLED, st.SUBMITTED)
    assert not st.can_transition(st.CANCELLED, st.FILLED)
    assert not st.can_transition(st.REJECTED, st.SUBMITTED)
    assert not st.can_transition(st.SUBMITTED, st.PENDING_SUBMIT)
    with pytest.raises(st.InvalidTransition):
        st.check_transition(st.FILLED, st.CANCELLED)


def test_status_normalisation_and_terminal():
    assert st.normalize("filled") == st.FILLED
    assert st.normalize("CANCELED") == st.CANCELLED
    assert st.is_terminal("rejected") and not st.is_terminal("UNKNOWN")


@pytest.mark.parametrize("venue,raw,expected", [
    ("BINANCE", "NEW", st.SUBMITTED), ("BINANCE", "PARTIALLY_FILLED", st.PARTIALLY_FILLED),
    ("BINANCE", "CANCELED", st.CANCELLED), ("BINANCE", "EXPIRED", st.CANCELLED),
    ("BINANCE", "REJECTED", st.REJECTED), ("BINANCE", "PENDING_CANCEL", st.SUBMITTED),
    ("BYBIT", "New", st.SUBMITTED), ("BYBIT", "PartiallyFilled", st.PARTIALLY_FILLED),
    ("BYBIT", "PartiallyFilledCanceled", st.CANCELLED), ("BYBIT", "Filled", st.FILLED),
    ("BYBIT", "Rejected", st.REJECTED), ("BINANCE", "SOMETHING_NEW", None), ("BYBIT", None, None),
])
def test_venue_status_mapping(venue, raw, expected):
    assert st.from_venue_status(venue, raw) == expected


async def _new(store, **kw):
    args = dict(instrument="BTCUSDT", side="BUY", order_type="LIMIT", quantity=Decimal("0.010"),
                price=Decimal("30000.50"), venue="BINANCE", user_id="u1")
    args.update(kw)
    return await store.create_live_order(**args)


@pytest.mark.asyncio
async def test_create_persists_pending_submit_with_exact_decimals(store):
    o = await _new(store)
    row = await store.get_order(o["id"])
    assert row["status"] == st.PENDING_SUBMIT and row["mode"] == "live"
    assert row["qty_str"] == "0.010" and row["price_str"] == "30000.50"
    assert len(row["client_order_id"]) <= 36 and row["client_order_id"].startswith("nw")
    assert [e["event"] for e in await store.list_events(o["id"])] == ["created"]


@pytest.mark.asyncio
async def test_transition_is_compare_and_set(store):
    o = await _new(store)
    ok = await store.transition(o["id"], st.SUBMITTED, expected=st.PENDING_SUBMIT, exchange_order_id="123")
    assert ok["status"] == st.SUBMITTED and ok["exchange_order_id"] == "123"
    # a writer holding a stale view loses the race
    assert await store.transition(o["id"], st.REJECTED, expected=st.PENDING_SUBMIT) is None
    assert (await store.get_order(o["id"]))["status"] == st.SUBMITTED


@pytest.mark.asyncio
async def test_terminal_orders_cannot_change_and_fills_never_decrease(store):
    o = await _new(store)
    await store.transition(o["id"], st.SUBMITTED)
    await store.transition(o["id"], st.PARTIALLY_FILLED, filled_qty=0.006)
    row = await store.transition(o["id"], st.PARTIALLY_FILLED, filled_qty=0.004)  # stale lower value
    assert row["filled_qty"] == pytest.approx(0.006)
    await store.transition(o["id"], st.FILLED, filled_qty=0.01)
    with pytest.raises(st.InvalidTransition):
        await store.transition(o["id"], st.CANCELLED)


@pytest.mark.asyncio
async def test_idempotency_key_is_unique_per_user(store):
    a = await _new(store, idempotency_key="k1")
    assert (await store.get_by_idempotency("u1", "k1"))["id"] == a["id"]
    assert await store.get_by_idempotency("u2", "k1") is None
    await _new(store, user_id="u2", idempotency_key="k1")  # same key, other user: fine
    import aiosqlite
    with pytest.raises(aiosqlite.IntegrityError):
        await _new(store, idempotency_key="k1")  # same user + key: rejected by the unique index


@pytest.mark.asyncio
async def test_unknown_columns_rejected(store):
    o = await _new(store)
    with pytest.raises(ValueError):
        await store.transition(o["id"], st.SUBMITTED, status="FILLED")
    with pytest.raises(ValueError):
        await store.update_fields(o["id"], quantity=5)


@pytest.mark.asyncio
async def test_open_live_list_and_notional(store):
    a = await _new(store)
    b = await _new(store, price=None, order_type="MARKET")
    c = await _new(store)
    await store.transition(c["id"], st.REJECTED)
    open_ids = {o["id"] for o in await store.list_open_live()}
    assert open_ids == {a["id"], b["id"]}
    assert await store.open_live_notional() == Decimal("0.010") * Decimal("30000.50")


@pytest.mark.asyncio
async def test_legacy_status_values_are_normalised_on_migrate(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "legacy.db")
    await database.init_db()
    async with database.connect() as db:
        await db.execute("INSERT INTO orders (id, instrument, side, status, timestamp) VALUES ('X1','A','BUY','filled','2026-01-01')")
        await db.execute("INSERT INTO orders (id, instrument, side, status, timestamp) VALUES ('X2','A','BUY','CANCELED','2026-01-01')")
        await db.commit()
    await database.init_db()
    assert (await order_store.get_order("X1"))["status"] == "FILLED"
    assert (await order_store.get_order("X2"))["status"] == "CANCELLED"
