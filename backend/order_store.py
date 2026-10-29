"""
Persistence for the order lifecycle (SQLite).

Adds lifecycle columns to the legacy `orders` table plus an append-only `order_events` table.
Status changes are compare-and-set (`UPDATE ... WHERE status = <expected>`), so two concurrent
writers (request handler, reconciler, cancel) can never both apply a stale transition.

Exact decimal quantities/prices are kept as strings (`qty_str`, `price_str`) — those are what is
sent to the venue. The legacy REAL columns stay for UI/aggregates.
"""

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import aiosqlite

import order_state

# Idempotent column migrations (SQLite has no ADD COLUMN IF NOT EXISTS)
_COLUMN_MIGRATIONS = [
    "ALTER TABLE orders ADD COLUMN client_order_id TEXT",
    "ALTER TABLE orders ADD COLUMN idempotency_key TEXT",
    "ALTER TABLE orders ADD COLUMN request_hash TEXT",
    "ALTER TABLE orders ADD COLUMN mode TEXT NOT NULL DEFAULT 'paper'",
    "ALTER TABLE orders ADD COLUMN venue TEXT",
    "ALTER TABLE orders ADD COLUMN qty_str TEXT",
    "ALTER TABLE orders ADD COLUMN price_str TEXT",
    "ALTER TABLE orders ADD COLUMN avg_price REAL",
    "ALTER TABLE orders ADD COLUMN last_error TEXT",
    "ALTER TABLE orders ADD COLUMN updated_at TEXT",
    "ALTER TABLE orders ADD COLUMN user_id TEXT",
    "ALTER TABLE orders ADD COLUMN check_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN cancel_requested_at TEXT",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id  TEXT NOT NULL,
    ts        TEXT NOT NULL,
    event     TEXT NOT NULL,
    status    TEXT,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_order_events_order ON order_events(order_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_client_id
    ON orders(client_order_id) WHERE client_order_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_idem
    ON orders(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_mode_status ON orders(mode, status);
"""

# Columns a transition may set
_MUTABLE = frozenset({
    "filled_qty", "avg_price", "exchange_order_id", "last_error", "pnl",
    "check_count", "cancel_requested_at", "venue",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def migrate(db: aiosqlite.Connection) -> None:
    """Called from database.init_db with an open connection."""
    for stmt in _COLUMN_MIGRATIONS:
        try:
            await db.execute(stmt)
            await db.commit()
        except aiosqlite.OperationalError:
            pass  # already exists
    await db.executescript(_SCHEMA)
    # Legacy rows wrote mixed-case statuses ('filled', 'CANCELED'); store upper-case only
    await db.execute("UPDATE orders SET status = 'CANCELLED' WHERE status IN ('CANCELED', 'canceled', 'cancelled')")
    await db.execute("UPDATE orders SET status = UPPER(status) WHERE status != UPPER(status)")
    await db.commit()


def new_client_order_id() -> str:
    """Venue-safe client order id: 'nw' + 32 hex = 34 chars (Binance/Bybit allow up to 36 of [A-Za-z0-9_-])."""
    return "nw" + uuid.uuid4().hex


def _connect():
    import database
    return database.connect()


async def _fetch_one(db: aiosqlite.Connection, sql: str, params: tuple) -> Optional[Dict[str, Any]]:
    db.row_factory = aiosqlite.Row
    async with db.execute(sql, params) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def _event(db: aiosqlite.Connection, order_id: str, event: str, status: Optional[str], detail: Any = None) -> None:
    await db.execute(
        "INSERT INTO order_events (order_id, ts, event, status, detail) VALUES (?, ?, ?, ?, ?)",
        (order_id, _now(), event, status, json.dumps(detail, default=str) if detail is not None else None),
    )


# ── Reads ─────────────────────────────────────────────────────────────────────

async def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    async with _connect() as db:
        return await _fetch_one(db, "SELECT * FROM orders WHERE id = ?", (order_id,))


async def get_by_client_id(client_order_id: str) -> Optional[Dict[str, Any]]:
    async with _connect() as db:
        return await _fetch_one(db, "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))


async def get_by_idempotency(user_id: str, key: str) -> Optional[Dict[str, Any]]:
    async with _connect() as db:
        return await _fetch_one(
            db, "SELECT * FROM orders WHERE user_id = ? AND idempotency_key = ?", (user_id, key)
        )


async def list_open_live() -> List[Dict[str, Any]]:
    """Live orders that have not reached a terminal state (the reconciler's work list)."""
    marks = ",".join("?" for _ in order_state.OPEN_LIVE)
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM orders WHERE mode = 'live' AND status IN ({marks}) ORDER BY timestamp",
            tuple(sorted(order_state.OPEN_LIVE)),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def list_events(order_id: str) -> List[Dict[str, Any]]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM order_events WHERE order_id = ? ORDER BY id", (order_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def count_today(mode: str) -> int:
    """Orders created today (UTC) in the given mode (a rejected attempt still counts: it used quota)."""
    today = datetime.now(timezone.utc).date().isoformat()
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM orders WHERE mode = ? AND date(timestamp) = ?", (mode, today)
        ) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else 0


async def open_live_notional() -> Decimal:
    """Sum of qty*price of non-terminal live LIMIT orders (market orders have no resting price)."""
    total = Decimal(0)
    for o in await list_open_live():
        if o.get("price_str") and o.get("qty_str"):
            remaining = Decimal(o["qty_str"]) - Decimal(str(o.get("filled_qty") or 0))
            if remaining > 0:
                total += remaining * Decimal(o["price_str"])
    return total


# ── Writes ────────────────────────────────────────────────────────────────────

class DuplicateClientOrderId(Exception):
    pass


async def create_live_order(
    *,
    instrument: str,
    side: str,
    order_type: str,
    quantity: Decimal,
    price: Optional[Decimal],
    venue: str,
    user_id: str,
    idempotency_key: Optional[str] = None,
    request_hash: Optional[str] = None,
    mode: str = "live",
) -> Dict[str, Any]:
    """Persist a live order as PENDING_SUBMIT *before* anything is sent to the venue."""
    order_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
    now = _now()
    row = {
        "id": order_id,
        "instrument": instrument,
        "side": side,
        "type": order_type,
        "quantity": float(quantity),
        "price": float(price) if price is not None else None,
        "status": order_state.PENDING_SUBMIT,
        "filled_qty": 0.0,
        "timestamp": now,
        "client_order_id": new_client_order_id(),
        "idempotency_key": idempotency_key,
        "request_hash": request_hash,
        "mode": mode,
        "venue": venue,
        "qty_str": format(quantity, "f"),
        "price_str": format(price, "f") if price is not None else None,
        "user_id": user_id,
        "updated_at": now,
    }
    cols = ", ".join(row)
    params = ", ".join(f":{k}" for k in row)
    async with _connect() as db:
        await db.execute(f"INSERT INTO orders ({cols}) VALUES ({params})", row)
        await _event(db, order_id, "created", order_state.PENDING_SUBMIT, {"venue": venue, "client_order_id": row["client_order_id"]})
        await db.commit()
    return row


async def transition(
    order_id: str,
    new_status: str,
    *,
    expected: Optional[str] = None,
    event: str = "status",
    detail: Any = None,
    **fields: Any,
) -> Optional[Dict[str, Any]]:
    """
    Move an order to `new_status` (compare-and-set on the current status).

    * `expected`: required current status; defaults to whatever is stored right now.
    * Illegal transitions raise order_state.InvalidTransition.
    * Returns the updated row, or None if another writer changed the status first (caller re-reads).
    * filled_qty never decreases; terminal orders are never changed.
    """
    bad = set(fields) - _MUTABLE
    if bad:
        raise ValueError(f"cannot set columns {sorted(bad)}")
    new_status = order_state.normalize(new_status)

    async with _connect() as db:
        current = await _fetch_one(db, "SELECT * FROM orders WHERE id = ?", (order_id,))
        if current is None:
            return None
        cur_status = order_state.normalize(current["status"])
        if expected is not None and cur_status != order_state.normalize(expected):
            return None
        order_state.check_transition(cur_status, new_status)

        sets = ["status = ?", "updated_at = ?"]
        params: List[Any] = [new_status, _now()]
        for k, v in fields.items():
            if k == "filled_qty":
                sets.append("filled_qty = MAX(COALESCE(filled_qty, 0), ?)")   # never decreases, even if racing
            else:
                sets.append(f"{k} = ?")
            params.append(v)
        params += [order_id, current["status"]]
        cur = await db.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE id = ? AND status = ?", params
        )
        if cur.rowcount == 0:
            await db.rollback()
            return None
        await _event(db, order_id, event, new_status, detail)
        await db.commit()
        return await _fetch_one(db, "SELECT * FROM orders WHERE id = ?", (order_id,))


async def add_event(order_id: str, event: str, detail: Any = None) -> None:
    async with _connect() as db:
        await _event(db, order_id, event, None, detail)
        await db.commit()


async def update_fields(order_id: str, **fields: Any) -> None:
    """Update non-status columns (e.g. check_count, last_error) without a transition."""
    bad = set(fields) - _MUTABLE
    if bad:
        raise ValueError(f"cannot set columns {sorted(bad)}")
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
    async with _connect() as db:
        await db.execute(f"UPDATE orders SET {sets} WHERE id = ?", [*fields.values(), _now(), order_id])
        await db.commit()
