"""
Paper-trading runtime: runs "active" strategies on real closed exchange candles with simulated fills.

Safety model: this module only ever READS public market data (Binance klines, no API key) and writes
to its own ledger (`paper_trades`, kv namespace `paper`). It never calls the order service, the
exchange clients or any credential, so it cannot place a real order whatever the trading mode is.

Behaviour
  * spot, long-only: a BUY signal opens a position of `trade_size` base units when flat, a SELL signal
    closes it; other signals are ignored
  * evaluates once per newly CLOSED candle; the first evaluation after (re)start only records the
    latest candle and never trades on history
  * fills at the signal candle's close, minus a taker fee (PAPER_FEE_RATE, default 0.1%)
  * orders larger than PAPER_MAX_NOTIONAL (default 10000, quote currency) are refused and reported
  * only strategies whose `instrument_id` is an exchange symbol such as BTCUSDT are executed; the
    simulated FX instruments (EUR/USD.SIM) stay backtest-only
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite
import httpx

import database
import strategy_signals

logger = logging.getLogger(__name__)

POLL_SECONDS = float(os.getenv("PAPER_POLL_SECONDS", "20"))
FEE_RATE = float(os.getenv("PAPER_FEE_RATE", "0.001"))
MAX_NOTIONAL = float(os.getenv("PAPER_MAX_NOTIONAL", "10000"))
KLINE_LIMIT = 300
INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}
_SYMBOL = re.compile(r"^[A-Z0-9]{5,15}$")
_NS = "paper"

_TRADES_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id           TEXT PRIMARY KEY,
    strategy_id  TEXT NOT NULL,
    instrument   TEXT NOT NULL,
    side         TEXT NOT NULL,
    quantity     REAL NOT NULL,
    price        REAL NOT NULL,
    fee          REAL NOT NULL,
    pnl          REAL,
    bar_time     INTEGER NOT NULL,
    created_at   TEXT NOT NULL
)
"""


def enabled() -> bool:
    return os.getenv("PAPER_RUNTIME_ENABLED", "1") == "1"


def is_executable(cfg: Dict[str, Any]) -> bool:
    """True when the runtime can run this strategy config (exchange symbol + known interval)."""
    return (enabled() and bool(_SYMBOL.match(str(cfg.get("instrument_id", ""))))
            and str(cfg.get("interval", "1m")) in INTERVALS)


def _base_url() -> str:
    return os.getenv("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/")


async def fetch_closed_candles(symbol: str, interval: str) -> List[Dict[str, float]]:
    """Closed candles, oldest first: [{"t": open_ms, "close": float}]. Raises on network/API errors."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_base_url()}/api/v3/klines",
                                params={"symbol": symbol, "interval": interval, "limit": KLINE_LIMIT})
    if resp.status_code != 200:
        raise RuntimeError(f"klines {symbol} {interval}: HTTP {resp.status_code}")
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    return [{"t": int(k[0]), "close": float(k[4])} for k in resp.json() if float(k[6]) < now_ms]


# ── persistence ──────────────────────────────────────────────────────────────

def _new_state() -> Dict[str, Any]:
    return {"position_qty": 0.0, "entry_price": 0.0, "entry_fee": 0.0, "last_bar": None,
            "realized_pnl": 0.0, "trades": 0, "closed_trades": 0, "wins": 0, "fees": 0.0,
            "last_error": None, "last_eval_at": None, "last_signal": None}


async def get_state(strategy_id: str) -> Dict[str, Any]:
    async with database.connect() as db:
        async with db.execute("SELECT value FROM kv_store WHERE namespace=? AND key=?", (_NS, strategy_id)) as cur:
            row = await cur.fetchone()
    return {**_new_state(), **json.loads(row[0])} if row else _new_state()


async def get_states() -> Dict[str, Dict[str, Any]]:
    async with database.connect() as db:
        async with db.execute("SELECT key, value FROM kv_store WHERE namespace=?", (_NS,)) as cur:
            return {k: {**_new_state(), **json.loads(v)} for k, v in await cur.fetchall()}


async def _save_state(strategy_id: str, state: Dict[str, Any]) -> None:
    async with database.connect() as db:
        await db.execute("INSERT OR REPLACE INTO kv_store (namespace, key, value) VALUES (?, ?, ?)",
                         (_NS, strategy_id, json.dumps(state)))
        await db.commit()


async def reseed(strategy_id: str) -> None:
    """On (re)start: forget the last seen candle so the next evaluation only re-seeds (no trade on a stale bar)."""
    state = await get_state(strategy_id)
    state["last_bar"] = None
    state["last_error"] = None
    await _save_state(strategy_id, state)


async def delete_state(strategy_id: str) -> None:
    async with database.connect() as db:
        await db.execute(_TRADES_SCHEMA)
        await db.execute("DELETE FROM kv_store WHERE namespace=? AND key=?", (_NS, strategy_id))
        await db.execute("DELETE FROM paper_trades WHERE strategy_id=?", (strategy_id,))
        await db.commit()


async def list_trades(strategy_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    async with database.connect() as db:
        await db.execute(_TRADES_SCHEMA)
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM paper_trades WHERE strategy_id=? ORDER BY bar_time DESC, created_at DESC LIMIT ?",
                              (strategy_id, limit)) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _record_trade(strategy_id: str, instrument: str, side: str, qty: float, price: float,
                        fee: float, pnl: Optional[float], bar_time: int) -> None:
    async with database.connect() as db:
        await db.execute(_TRADES_SCHEMA)
        await db.execute(
            "INSERT INTO paper_trades (id, strategy_id, instrument, side, quantity, price, fee, pnl, bar_time, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"PT-{uuid.uuid4().hex[:8].upper()}", strategy_id, instrument, side, qty, price, fee, pnl,
             bar_time, datetime.now(timezone.utc).isoformat()))
        await db.commit()


# ── evaluation ───────────────────────────────────────────────────────────────

async def evaluate(strategy: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate one active strategy once. Returns its updated state. Never raises."""
    sid, cfg = strategy["id"], strategy["config"]
    state = await get_state(sid)
    state["last_eval_at"] = datetime.now(timezone.utc).isoformat()
    try:
        symbol, interval = str(cfg["instrument_id"]), str(cfg.get("interval", "1m"))
        candles = await fetch_closed_candles(symbol, interval)
        if not candles:
            raise RuntimeError("no closed candles returned")
        latest = candles[-1]
        state["last_error"] = None
        if state["last_bar"] is None:
            state["last_bar"] = latest["t"]            # first run: remember, never trade on history
        elif latest["t"] > state["last_bar"]:
            state["last_bar"] = latest["t"]
            signal = strategy_signals.signal_for(strategy["type"], [c["close"] for c in candles], cfg)
            state["last_signal"] = signal
            if signal:
                state["last_error"] = await _apply_signal(strategy, state, signal, latest)
    except Exception as exc:  # noqa: BLE001 - one strategy must not stop the others
        state["last_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        logger.warning("paper strategy %s: %s", sid, state["last_error"])
    await _save_state(sid, state)
    return state


async def _apply_signal(strategy: Dict[str, Any], state: Dict[str, Any], signal: str,
                        candle: Dict[str, float]) -> Optional[str]:
    """Apply a signal to the simulated position. Returns an error message when the order is refused."""
    sid, cfg = strategy["id"], strategy["config"]
    symbol, price = str(cfg["instrument_id"]), candle["close"]
    holding = state["position_qty"] > 0
    if signal == strategy_signals.BUY and not holding:
        qty = float(cfg.get("trade_size", 0) or 0)
        if qty <= 0:
            return "trade_size must be positive"
        if qty * price > MAX_NOTIONAL:
            return (f"order value {qty * price:.2f} exceeds the paper cap {MAX_NOTIONAL:.0f}; "
                    f"lower trade_size (base units of {symbol})")
        fee = qty * price * FEE_RATE
        state.update(position_qty=qty, entry_price=price, entry_fee=fee)
        state["fees"] += fee
        state["trades"] += 1
        await _record_trade(sid, symbol, "BUY", qty, price, fee, None, candle["t"])
    elif signal == strategy_signals.SELL and holding:
        qty = state["position_qty"]
        fee = qty * price * FEE_RATE
        pnl = (price - state["entry_price"]) * qty - fee - state["entry_fee"]
        state["realized_pnl"] += pnl
        state["fees"] += fee
        state["trades"] += 1
        state["closed_trades"] += 1
        state["wins"] += 1 if pnl > 0 else 0
        state.update(position_qty=0.0, entry_price=0.0, entry_fee=0.0)
        await _record_trade(sid, symbol, "SELL", qty, price, fee, pnl, candle["t"])
    return None


async def active_strategies() -> List[Dict[str, Any]]:
    out = []
    for row in await database.list_strategies():
        if row.get("status") != "running":
            continue
        try:
            cfg = json.loads(row.get("config") or "{}")
        except (TypeError, ValueError):
            continue
        if is_executable(cfg):
            out.append({"id": row["id"], "type": row["type"], "config": cfg})
    return out


async def run_once() -> int:
    """One round over every executable active strategy. Returns how many were evaluated."""
    strategies = await active_strategies()
    for s in strategies:
        await evaluate(s)
    return len(strategies)


async def runtime_loop() -> None:
    if not enabled():
        logger.info("paper runtime disabled (PAPER_RUNTIME_ENABLED=0)")
        return
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("paper runtime round failed")
        await asyncio.sleep(POLL_SECONDS)
