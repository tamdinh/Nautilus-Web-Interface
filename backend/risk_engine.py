"""
Risk enforcement engine — Sprint 3 (S3-03).

Checks risk limits before allowing any order to be created.
All four checks must pass; the first failure raises RiskCheckError (HTTP 422).
"""

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import HTTPException

# Limits that must all be configured (> 0) before any live order is accepted
LIVE_REQUIRED_LIMITS = ("max_position_size", "max_daily_loss", "max_orders_per_day")


class RiskCheckError(HTTPException):
    """Raised when an order violates a risk limit."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            status_code=422,
            detail={"error": "risk_limit_exceeded", "message": detail},
        )


class RiskEngine:
    """Stateless risk checker — reads limits live from DB on each call."""

    async def check_order(self, order: Dict[str, Any]) -> None:
        """
        Run all risk checks for the given order dict.
        Raises RiskCheckError if any check fails.
        """
        import database

        limits = await database.get_risk_limits()

        await self._check_max_position_size(order, limits)
        await self._check_daily_loss_limit(limits)
        await self._check_leverage(order, limits)
        await self._check_orders_per_day(limits)

    # ── Live (real-money) path: strict and fail-closed ────────────────────────

    @staticmethod
    def _pos(value: Any) -> Optional[Decimal]:
        try:
            d = Decimal(str(value))
        except Exception:
            return None
        return d if d > 0 else None

    async def live_readiness_problems(self) -> list:
        """Reasons live trading may not be enabled yet (empty list = limits are complete)."""
        import database
        limits = await database.get_risk_limits()
        problems = [f"risk limit '{k}' must be set to a positive value"
                    for k in LIVE_REQUIRED_LIMITS if self._pos(limits.get(k)) is None]
        if not await database.risk_limits_explicitly_set():
            problems.append("risk limits have never been reviewed and saved by an admin (defaults are not enough)")
        return problems

    async def check_live_order(
        self,
        *,
        notional: Decimal,
        leverage: float,
        equity_fn: Callable[[], Awaitable[Optional[Decimal]]],
        venue: str = "default",
    ) -> None:
        """
        Check a real order. Unlike the paper path nothing is skipped: every required limit must be
        configured, the order must be valued, and if the account equity cannot be read the order is
        refused. The caller must hold the order-placement lock so the counts below include any
        order that is still being submitted.
        """
        import database
        import order_store
        import trading_control

        limits = await database.get_risk_limits()
        problems = [k for k in LIVE_REQUIRED_LIMITS if self._pos(limits.get(k)) is None]
        if not await database.risk_limits_explicitly_set():
            problems.append("(limits not saved by an admin)")
        if problems:
            raise RiskCheckError(f"live trading requires risk limits to be configured: {', '.join(problems)}")

        max_order_value = self._pos(limits["max_position_size"])
        if notional > max_order_value:
            raise RiskCheckError(
                f"Order value {notional:.2f} exceeds max_position_size limit of {max_order_value:.2f}")

        if leverage and float(leverage) > 1.0:
            raise RiskCheckError("leverage above 1x is not supported for spot trading")

        max_orders = int(limits["max_orders_per_day"])
        today_count = await order_store.count_today("live")
        if today_count >= max_orders:
            raise RiskCheckError(
                f"Daily order limit of {max_orders} reached ({today_count} live orders placed today).")

        max_open = self._pos(limits.get("max_open_exposure"))
        if max_open is not None:
            resting = await order_store.open_live_notional()
            if resting + notional > max_open:
                raise RiskCheckError(
                    f"Open exposure {resting + notional:.2f} would exceed max_open_exposure limit of {max_open:.2f}")

        # Daily loss = drop of account equity since the first check of the UTC day
        max_loss = self._pos(limits["max_daily_loss"])
        equity = await equity_fn()
        if equity is None:
            raise RiskCheckError("account equity could not be read; refusing to trade without a loss check")
        start = await self._day_start_equity(equity, venue)
        loss = start - equity
        if loss >= max_loss:
            # disarm AND take back any order still resting / in flight (reconciler honours cancel_pending)
            await trading_control.disarm(
                "risk-engine", f"daily loss {loss:.2f} reached limit {max_loss:.2f} on {venue}", cancel_open=True)
            await self._auto_stop_strategies_on_loss(float(loss), float(max_loss))
            raise RiskCheckError(
                f"Daily loss {loss:.2f} reached max_daily_loss limit of {max_loss:.2f}. "
                "Trading halted (kill switch engaged).")

    async def _day_start_equity(self, current: Decimal, venue: str = "default") -> Decimal:
        """
        Equity recorded at the first live check of the current UTC day for this venue (stored, survives
        restarts). One baseline per venue: equities of different exchanges must never be compared.
        """
        import database
        today = datetime.now(timezone.utc).date().isoformat()
        key = f"day_start_equity:{venue}"
        async with database.connect() as db:
            async with db.execute(
                "SELECT value FROM kv_store WHERE namespace='risk' AND key=?", (key,)) as cur:
                row = await cur.fetchone()
            if row:
                try:
                    rec = json.loads(row[0])
                    if rec.get("date") == today:
                        return Decimal(rec["equity"])
                except (ValueError, KeyError, TypeError):
                    pass
            await db.execute(
                "INSERT OR REPLACE INTO kv_store (namespace, key, value) VALUES ('risk', ?, ?)",
                (key, json.dumps({"date": today, "equity": str(current)})))
            await db.commit()
        return current

    # ── Individual checks (paper path) ────────────────────────────────────────

    async def _check_max_position_size(
        self, order: Dict[str, Any], limits: Dict[str, Any]
    ) -> None:
        max_pos = float(limits.get("max_position_size", 0))
        if not max_pos or max_pos <= 0:
            return  # Unlimited

        quantity = float(order.get("quantity", 0))
        price = float(order.get("price") or 0)

        # Only check if price is explicitly provided; MARKET orders without
        # a price cannot be reliably valued at submission time.
        if price == 0:
            return

        order_value = quantity * price
        if order_value > max_pos:
            raise RiskCheckError(
                f"Order value {order_value:.2f} exceeds max_position_size limit of {max_pos:.2f}"
            )

    async def _check_daily_loss_limit(self, limits: Dict[str, Any]) -> None:
        max_loss = float(limits.get("max_daily_loss", 0))
        if not max_loss or max_loss <= 0:
            return  # Unlimited

        import database

        daily_loss = await database.get_daily_realized_loss()
        if abs(daily_loss) > max_loss:
            # Auto-stop all running strategies
            await self._auto_stop_strategies_on_loss(daily_loss, max_loss)
            raise RiskCheckError(
                f"Daily realized loss {abs(daily_loss):.2f} exceeds "
                f"max_daily_loss limit of {max_loss:.2f}. Trading halted until tomorrow."
            )

    async def _auto_stop_strategies_on_loss(
        self, daily_loss: float, max_loss: float
    ) -> None:
        """Stop all running strategies when daily loss limit is breached."""
        try:
            import database
            from state import nautilus_system
            strategies = await database.list_strategies()
            for s in strategies:
                if s.get("status") == "running":
                    await database.update_strategy_status(s["id"], "stopped")
                    if s["id"] in nautilus_system.strategies:
                        nautilus_system.strategies[s["id"]]["status"] = "stopped"
        except Exception:
            pass  # Best-effort; never block the main risk check

    async def _check_leverage(
        self, order: Dict[str, Any], limits: Dict[str, Any]
    ) -> None:
        max_lev = float(limits.get("max_leverage", 0))
        if not max_lev or max_lev <= 0:
            return  # Unlimited

        order_leverage = float(order.get("leverage", 1.0))
        if order_leverage > max_lev:
            raise RiskCheckError(
                f"Order leverage {order_leverage}x exceeds max_leverage limit of {max_lev}x"
            )

    async def _check_orders_per_day(self, limits: Dict[str, Any]) -> None:
        max_orders = int(limits.get("max_orders_per_day", 0))
        if not max_orders or max_orders <= 0:
            return  # Unlimited

        import database

        today_count = await database.count_orders_today()
        if today_count >= max_orders:
            raise RiskCheckError(
                f"Daily order limit of {max_orders} reached "
                f"({today_count} orders placed today)."
            )


    async def check_daily_loss_auto_stop(self) -> None:
        """
        Check daily loss limit and auto-stop all running strategies if exceeded.
        Called from list_strategies to ensure state is up-to-date.
        Does NOT raise — only auto-stops strategies as a side-effect.
        """
        try:
            import database
            limits = await database.get_risk_limits()
            max_loss = float(limits.get("max_daily_loss", 0))
            if not max_loss or max_loss <= 0:
                return
            daily_loss = await database.get_daily_realized_loss()
            if abs(daily_loss) > max_loss:
                await self._auto_stop_strategies_on_loss(daily_loss, max_loss)
        except Exception:
            pass  # Best-effort


# ── Module-level singleton ────────────────────────────────────────────────────

risk_engine = RiskEngine()
