"""
Positions router.

Open positions are read from the DB positions table (persisted after each
backtest run).  The UI-layer close action marks them as closed in the DB.
Current prices are enriched from market_data_service when the instrument
matches a supported crypto symbol (e.g. BTCUSDT).
"""

import asyncio
import logging
from typing import Any, Dict, List

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException

import database
from auth_jwt import require_trader
import market_data_service as svc
from order_service import order_service
from state import live_manager, nautilus_system

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["positions"])


async def _enrich_current_prices(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Best-effort: add current_price and live unrealized_pnl to each position."""
    if not positions:
        return positions

    # Gather unique symbols that market_data_service knows about
    symbols_needed = {
        p["instrument"].upper()
        for p in positions
        if p.get("instrument", "").upper() in svc.SYMBOLS
    }

    if not symbols_needed:
        return positions

    # Fetch all needed prices concurrently
    price_map: Dict[str, float] = {}
    async def _fetch(sym: str) -> None:
        try:
            data = await svc.get_symbol_data(sym)
            price_map[sym] = data.get("price", 0.0)
        except Exception:
            pass  # keep whatever is already in DB

    await asyncio.gather(*[_fetch(s) for s in symbols_needed])

    enriched = []
    for pos in positions:
        p = dict(pos)
        sym = p.get("instrument", "").upper()
        if sym in price_map and price_map[sym] > 0:
            current = price_map[sym]
            p["current_price"] = current
            # Recompute unrealized PnL when entry_price is available
            entry = float(p.get("entry_price") or 0)
            qty = float(p.get("quantity") or 0)
            if entry > 0 and qty > 0:
                side = str(p.get("side", "")).upper()
                multiplier = 1.0 if "LONG" in side or "BUY" in side else -1.0
                p["pnl"] = round((current - entry) * qty * multiplier, 4)
        enriched.append(p)
    return enriched


@router.get("/positions")
async def list_positions():
    # Primary: DB-persisted positions (survive restarts, reflect latest backtest)
    db_positions = await database.list_db_positions(open_only=True)

    # Determine data source
    source = "live" if live_manager.is_connected() else "cached"

    if db_positions:
        enriched = await _enrich_current_prices(db_positions)
        for pos in enriched:
            pos["source"] = source
        return enriched

    # Fallback: in-memory backtest results (before first backtest persists to DB)
    all_positions = []
    for results in nautilus_system.backtest_results.values():
        all_positions.extend(results.get("positions", []))
    open_pos = [p for p in all_positions if p.get("is_open", False)]
    enriched = await _enrich_current_prices(open_pos)
    for pos in enriched:
        pos["source"] = source
    return enriched


@router.post("/positions/sync")
async def sync_positions(_user: dict = Depends(require_trader)):
    """Sync open positions from the connected exchange(s). Replaces the stored snapshot per venue."""
    live_positions = await live_manager.sync_positions()

    by_venue: Dict[str, List[Dict[str, Any]]] = {}
    for p in live_positions:
        by_venue.setdefault(str(p.get("exchange", "UNKNOWN")), []).append(p)
    for venue in live_manager.ready_venues():
        # a venue that is connected but returned nothing has no open balances: clear its snapshot
        await database.replace_live_positions(venue, by_venue.get(venue, []))

    return {
        "success": True,
        "synced_count": len(live_positions),
        "positions": live_positions,
    }


@router.post("/positions/{position_id}/close")
async def close_position(position_id: str, _user: dict = Depends(require_trader)):
    """
    Close a position.

    Exchange-synced positions are closed by submitting a real MARKET order through the normal order
    path (mode/arm check, exchange filters, risk check, client order id, audit). The position is NOT
    marked closed here: it disappears on the next sync once the exchange balance is gone. Unknown or
    zero-quantity positions are refused instead of guessed at.
    """
    positions = await database.list_db_positions(open_only=False)
    target = next((p for p in positions if p["id"] == position_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Position {position_id} not found")

    tag = str(target.get("strategy_id") or "")
    if tag.startswith("live:"):
        import time
        import trading_control
        if (await trading_control.get_state())["mode"] != "live":
            raise HTTPException(status_code=409, detail={
                "error": "not_live", "message": "Exchange positions can only be closed in live mode."})
        venue = tag.split(":", 1)[1]
        instrument = str(target.get("instrument") or "")
        if not instrument or instrument.upper() == "UNKNOWN":
            raise HTTPException(status_code=422, detail="Position has no valid instrument: refusing to close")
        pos_side = str(target.get("side", "LONG")).upper()
        close_side = "SELL" if ("LONG" in pos_side or "BUY" in pos_side) else "BUY"
        if close_side != "SELL":
            raise HTTPException(status_code=422, detail="Only long spot positions can be closed")
        try:
            symbol = live_manager.to_venue_symbol(instrument)
            quote = next(q for q in ("USDT", "USDC", "FDUSD", "BUSD") if symbol.endswith(q))
            # sell what the exchange says is free right now (locked funds and dust are not sellable)
            quantity = await live_manager.free_balance(venue, symbol[: -len(quote)])
        except (StopIteration, ValueError):
            raise HTTPException(status_code=422, detail="Cannot derive the asset to sell from this instrument")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"cannot read the {venue} balance: {exc}")
        if quantity <= 0:
            raise HTTPException(status_code=422, detail="No free balance to close")
        # Double clicks within 30s resolve to one order (same key + same payload = replay)
        key = f"close-{position_id}-{int(time.time() // 30)}"
        result = await order_service.place(
            user=_user.get("sub", ""),
            order={"instrument": instrument, "side": close_side, "type": "MARKET", "quantity": quantity,
                   "price": None, "leverage": 1.0, "venue": venue, "floor_quantity": True},
            idempotency_key=key[:64])
        await database.log_action(action="position_close_requested", user_id=_user.get("sub", ""),
                                  resource=f"position:{position_id}",
                                  details=f"order={result.get('order', {}).get('id')}")
        return {"success": True, "closed_in_db": False, "order": result.get("order"),
                "message": f"Close order submitted for {position_id}; position clears on next sync"}

    closed = await database.close_db_position(position_id)
    return {"success": True, "closed_in_db": closed, "message": f"Position {position_id} closed"}
