"""
Trading mode and kill switch (persistent, fail-closed).

* mode: "paper" (default) or "live". Orders follow the mode explicitly — a connected exchange
  adapter alone never makes an order real.
* trading_enabled (the arm/kill switch): live orders are refused unless this is True. Default False,
  and it is forced back to False after every process restart (an operator must re-arm), so a
  crashed/redeployed backend never resumes live trading by itself.
* Every change is audited (who/when/why) in the state record itself; the API layer also writes the
  audit log.

State lives in kv_store (namespace 'trading') and is read from the database on every check, so the
kill switch takes effect on the very next order — there is no in-process cache to go stale.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import database

MODES = ("paper", "live")


class TradingDisabled(Exception):
    """Live trading is not currently allowed (kill switch engaged / not armed / wrong mode)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _get(key: str) -> Optional[str]:
    async with database.connect() as db:
        async with db.execute("SELECT value FROM kv_store WHERE namespace='trading' AND key=?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def _set(db, key: str, value: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO kv_store (namespace, key, value) VALUES ('trading', ?, ?)", (key, value))


async def get_state() -> Dict[str, Any]:
    meta_raw = await _get("meta")
    try:
        meta = json.loads(meta_raw) if meta_raw else {}
    except (TypeError, ValueError):
        meta = {}
    mode = await _get("mode")
    return {
        "mode": mode if mode in MODES else "paper",
        "trading_enabled": (await _get("enabled")) == "1",
        # set by a kill with cancel_open: orders that only show up on the venue later get cancelled too
        "cancel_pending": (await _get("cancel_pending")) == "1",
        "changed_by": meta.get("by", ""),
        "changed_at": meta.get("at", ""),
        "reason": meta.get("reason", ""),
    }


async def _write(user: str, reason: str, *, mode: Optional[str] = None, enabled: Optional[bool] = None,
                 cancel_pending: Optional[bool] = None) -> Dict[str, Any]:
    async with database.connect() as db:
        if mode is not None:
            await _set(db, "mode", mode)
        if enabled is not None:
            await _set(db, "enabled", "1" if enabled else "0")
        if cancel_pending is not None:
            await _set(db, "cancel_pending", "1" if cancel_pending else "0")
        await _set(db, "meta", json.dumps({"by": user, "at": _now(), "reason": reason}))
        await db.commit()
    return await get_state()


async def set_mode(mode: str, user: str, reason: str = "") -> Dict[str, Any]:
    """Switch paper/live. Leaving live (or entering it) always disarms the switch."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    return await _write(user, reason or f"mode -> {mode}", mode=mode, enabled=False, cancel_pending=False)


async def arm(user: str, reason: str = "armed") -> Dict[str, Any]:
    """Allow live orders. Only valid in live mode."""
    state = await get_state()
    if state["mode"] != "live":
        raise TradingDisabled("cannot arm: trading mode is not 'live'")
    return await _write(user, reason, enabled=True, cancel_pending=False)


async def disarm(user: str, reason: str, cancel_open: bool = False) -> Dict[str, Any]:
    """
    Kill switch: refuse every further live order. With `cancel_open`, orders that are still being
    submitted or whose outcome is unknown are cancelled too as soon as they turn up on the venue.
    """
    return await _write(user, reason or "kill switch", enabled=False,
                        cancel_pending=True if cancel_open else None)


async def reset_on_startup() -> Dict[str, Any]:
    """Fail-closed after restart: always come up disarmed (set TRADING_AUTO_ARM=1 to opt out)."""
    state = await get_state()
    if state["trading_enabled"] and os.getenv("TRADING_AUTO_ARM", "0") != "1":
        return await _write("system", "disarmed on restart (re-arm required)", enabled=False)
    return state


async def assert_live_allowed() -> Dict[str, Any]:
    """Raise TradingDisabled unless mode == live AND the switch is armed."""
    state = await get_state()
    if state["mode"] != "live":
        raise TradingDisabled("trading mode is 'paper': live orders are not allowed")
    if not state["trading_enabled"]:
        raise TradingDisabled(
            "live trading is disarmed"
            + (f" ({state['reason']})" if state.get("reason") else "")
            + ": an admin must re-arm it")
    return state
