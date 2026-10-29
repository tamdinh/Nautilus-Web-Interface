"""
Order lifecycle service: paper and live order placement, cancel, reconcile, kill switch.

Live placement is built so that no failure can silently create or lose an order:

  1. mode/arm check  → 2. venue resolved (no fallback) → 3. exchange filters validated
  4. risk check + persist PENDING_SUBMIT (under one lock, so in-flight orders count)
  5. send ONE attempt carrying our client order id
  6. classify the outcome:
       accepted  → SUBMITTED / FILLED ...           rejected → REJECTED (venue certainly refused)
       not_sent  → REJECTED (nothing left the box)   unknown  → UNKNOWN, looked up by client id
  7. the reconciler keeps following every non-terminal live order until the venue gives a final answer

Nothing is retried blindly: a timeout never becomes "failed, please retry" — it becomes UNKNOWN and is
resolved by asking the venue about our client order id.
"""

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import aiosqlite
from fastapi import HTTPException

import database
import order_state
import ops_alerts
import order_store
import trading_control
from exchange_clients import (
    ExchangeError,
    ExchangeHTTPError,
    ExchangeRateLimited,
    ExchangeTransportError,
)
from order_rules import OrderValidationError, validate_order
from risk_engine import RiskCheckError, risk_engine

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL = float(os.getenv("RECONCILE_INTERVAL_SECS", "15"))
# An order whose submit outcome is unknown is only declared "never reached the venue" after this
# long AND this many consecutive "not found" answers (exchange state can lag by seconds).
NOT_FOUND_GRACE_SECS = float(os.getenv("ORDER_NOT_FOUND_GRACE_SECS", "120"))
NOT_FOUND_MIN_CHECKS = 3
LOOKUP_ATTEMPTS = 3
STUCK_UNKNOWN_SECONDS = float(os.getenv("ORDER_STUCK_UNKNOWN_SECS", "300"))
LOOKUP_DELAY = float(os.getenv("ORDER_LOOKUP_DELAY_SECS", "0.5"))
# Risk limits are denominated in USD-like units, so live trading is limited to pairs quoted in them
STABLE_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD")


def _err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message, **extra})


def _request_hash(order: Dict[str, Any]) -> str:
    canon = "|".join(str(order.get(k) or "") for k in ("instrument", "side", "type", "quantity", "price", "venue"))
    return hashlib.sha256(canon.encode()).hexdigest()


def _age_secs(row: Dict[str, Any]) -> float:
    try:
        ts = datetime.fromisoformat(row["timestamp"])
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except (KeyError, ValueError, TypeError):
        return 0.0


class OrderService:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._place_lock = asyncio.Lock()      # risk check + PENDING_SUBMIT insert are one critical section
        self._reconcile_task: Optional[asyncio.Task] = None

    # ═════════════════════════════════════════════════════════════════════════
    # Placement
    # ═════════════════════════════════════════════════════════════════════════

    async def place(self, *, user: str, order: Dict[str, Any], idempotency_key: Optional[str]) -> Dict[str, Any]:
        """
        `order` keys: instrument, side, type, quantity (Decimal), price (Decimal|None), leverage, venue (optional).
        Returns {"success": True, "order": row, ...}; raises HTTPException on refusal.
        """
        state = await trading_control.get_state()
        if state["mode"] != "live":
            return await self._place_paper(user, order)
        return await self._place_live(user, order, idempotency_key)

    # ── paper ─────────────────────────────────────────────────────────────────

    async def _place_paper(self, user: str, order: Dict[str, Any]) -> Dict[str, Any]:
        paper = {**order, "quantity": float(order["quantity"]),
                 "price": float(order["price"]) if order.get("price") is not None else None}
        await risk_engine.check_order(paper)  # RiskCheckError -> 422

        # Without any explicitly configured risk limits, non-simulated instruments need an adapter
        # (pristine/demo behaviour); once limits are set, paper trading is allowed on any instrument.
        if not self.manager.is_connected() and not str(order["instrument"]).endswith(".SIM"):
            if not await database.risk_limits_explicitly_set():
                raise HTTPException(
                    status_code=400,
                    detail="No adapter connected. Connect an exchange adapter before placing live orders.",
                )

        row = await database.create_order(
            instrument=order["instrument"], side=order["side"], order_type=order["type"],
            quantity=paper["quantity"], price=paper["price"],
        )
        await database.log_action(
            action="order_created", user_id=user, resource=f"order:{row['id']}",
            details=f"mode=paper instrument={order['instrument']} side={order['side']} "
                    f"qty={order['quantity']} price={order.get('price')}")
        return {"success": True, "order": row, "mode": "paper"}

    # ── live ──────────────────────────────────────────────────────────────────

    async def _place_live(self, user: str, order: Dict[str, Any], idempotency_key: Optional[str]) -> Dict[str, Any]:
        try:
            await trading_control.assert_live_allowed()
        except trading_control.TradingDisabled as exc:
            raise _err(409, "trading_disabled", str(exc))

        instrument = str(order["instrument"])
        if instrument.upper().endswith(".SIM"):
            raise _err(400, "invalid_instrument", "Simulated (.SIM) instruments cannot be traded in live mode")
        otype = str(order["type"]).upper()
        if otype not in ("MARKET", "LIMIT"):
            raise _err(422, "unsupported_order_type", f"{otype} orders are not supported in live mode")
        side = str(order["side"]).upper()

        # "BTCUSDT.BYBIT" names its venue: it must agree with an explicit venue and is never ignored
        suffix = instrument.split(".")[1].upper() if "." in instrument else None
        requested = str(order.get("venue") or "").upper() or None
        if suffix and requested and suffix != requested:
            raise _err(400, "venue_mismatch", f"instrument says {suffix} but venue {requested} was requested")
        try:
            venue = self.manager.resolve_venue(requested or suffix)
            symbol = self.manager.to_venue_symbol(instrument)
        except ValueError as exc:
            raise _err(400, "invalid_instrument", str(exc))
        except RuntimeError as exc:
            raise _err(503, "venue_unavailable", str(exc))  # live never falls back to paper
        if not symbol.endswith(STABLE_QUOTES):
            raise _err(422, "unsupported_quote",
                       f"{symbol}: live trading supports pairs quoted in {', '.join(STABLE_QUOTES)} only "
                       "(risk limits are valued in USD)")

        qty: Decimal = Decimal(str(order["quantity"]))
        price: Optional[Decimal] = Decimal(str(order["price"])) if order.get("price") not in (None, 0, "0") else None
        order = {**order, "venue": venue, "price": price}

        # Idempotent replay
        req_hash = _request_hash({**order, "instrument": symbol, "quantity": qty})
        if idempotency_key:
            existing = await order_store.get_by_idempotency(user, idempotency_key)
            if existing:
                return self._replay(existing, req_hash)

        # Venue filters + reference price (needed to value MARKET orders). Failure to load them
        # means we cannot validate: refuse rather than send an unchecked order.
        try:
            rules = await self.manager.rules(venue, symbol)
            ref_price = await self.manager.reference_price(venue, symbol) if otype == "MARKET" else None
        except ExchangeHTTPError as exc:
            if exc.is_not_found or exc.status == 400:
                raise _err(400, "unknown_symbol", f"{symbol} is not available on {venue}: {exc}")
            raise _err(503, "venue_unavailable", f"cannot load {venue} symbol rules: {exc}")
        except ExchangeError as exc:
            raise _err(503, "venue_unavailable", f"cannot load {venue} symbol rules: {exc}")
        if order.get("floor_quantity"):
            # closing a whole balance: round DOWN to the venue's step (dust below one step stays)
            step = (rules.market_step_size if otype == "MARKET" else None) or rules.step_size
            if step and step > 0:
                qty = (qty // step) * step
            if qty <= 0:
                raise _err(422, "nothing_to_close", "balance is below the venue's minimum tradable step")
            req_hash = _request_hash({**order, "instrument": symbol, "quantity": qty})
        if otype == "MARKET" and (ref_price is None or ref_price <= 0):
            raise _err(503, "venue_unavailable", f"no usable reference price for {symbol}: refusing to send")
        try:
            qty_str, price_str = validate_order(rules, side, otype, qty, price, ref_price)
        except OrderValidationError as exc:
            raise _err(422, "invalid_order", str(exc))

        notional = qty * (price if price is not None else ref_price)

        async with self._place_lock:
            try:  # the kill switch may have been pulled while we were talking to the venue
                await trading_control.assert_live_allowed()
            except trading_control.TradingDisabled as exc:
                raise _err(409, "trading_disabled", str(exc))
            try:
                await risk_engine.check_live_order(
                    notional=notional, leverage=float(order.get("leverage") or 1.0), venue=venue,
                    equity_fn=lambda: self.manager.equity_usdt(venue))
            except RiskCheckError:
                raise
            try:
                row = await order_store.create_live_order(
                    instrument=symbol, side=side, order_type=otype, quantity=qty, price=price,
                    venue=venue, user_id=user, idempotency_key=idempotency_key, request_hash=req_hash)
            except aiosqlite.IntegrityError:
                # concurrent request with the same idempotency key won the insert
                existing = await order_store.get_by_idempotency(user, idempotency_key or "")
                if existing:
                    return self._replay(existing, req_hash)
                raise

        await database.log_action(
            action="order_created", user_id=user, resource=f"order:{row['id']}",
            details=f"mode=live venue={venue} symbol={symbol} side={side} type={otype} qty={qty_str} "
                    f"price={price_str} client_order_id={row['client_order_id']}")

        # From here on the order exists: a client disconnect (task cancellation) must not abandon it
        # between "persisted" and "sent/recorded", so the send runs as its own task.
        task = asyncio.ensure_future(self._submit(row, qty_str, price_str))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # mark exception as retrieved
        return await asyncio.shield(task)

    @staticmethod
    def _replay(existing: Dict[str, Any], req_hash: str) -> Dict[str, Any]:
        if existing.get("request_hash") and existing["request_hash"] != req_hash:
            raise _err(409, "idempotency_conflict",
                       "this Idempotency-Key was already used with a different order")
        return {"success": True, "order": existing, "mode": "live", "idempotent_replay": True}

    async def _submit(self, row: Dict[str, Any], qty_str: str, price_str: Optional[str]) -> Dict[str, Any]:
        """Send the one attempt and record its outcome."""
        order_id = row["id"]
        venue, symbol = row["venue"], row["instrument"]
        try:  # last gate: the kill switch wins over an order that has not left the building yet
            await trading_control.assert_live_allowed()
        except trading_control.TradingDisabled as exc:
            await order_store.transition(order_id, order_state.REJECTED, expected=order_state.PENDING_SUBMIT,
                                         event="not_sent", detail={"error": str(exc)}, last_error=str(exc)[:500])
            raise _err(409, "trading_disabled", str(exc), order_id=order_id)
        try:
            result = await self.manager.send_order(
                venue, symbol=symbol, side=row["side"], order_type=row["type"], qty=qty_str,
                price=price_str, client_order_id=row["client_order_id"])
        except (ExchangeHTTPError, ExchangeTransportError, ExchangeRateLimited) as exc:
            return await self._on_submit_error(row, exc)
        except Exception as exc:  # noqa: BLE001 - we may or may not have sent: never assume
            logger.exception("unexpected error submitting %s", order_id)
            return await self._mark_unknown(row, f"unexpected error: {exc!r}")

        status = order_state.from_venue_status(venue, result.get("raw_status")) or order_state.SUBMITTED
        updated = await order_store.transition(
            order_id, status, expected=order_state.PENDING_SUBMIT, event="submitted",
            detail={"venue_order_id": result.get("venue_order_id")},
            exchange_order_id=result.get("venue_order_id") or None,
            filled_qty=float(result.get("executed_qty") or 0),
            avg_price=float(result["avg_price"]) if result.get("avg_price") else None)
        if updated is None:  # reconciler already moved it
            updated = await order_store.get_order(order_id)
        # Kill pulled while the request was in flight: the order is live on the venue, take it back
        if (await trading_control.get_state())["cancel_pending"] and not order_state.is_terminal(updated["status"]):
            updated = await self._kill_cancel(updated) or updated
        return {"success": True, "order": updated, "mode": "live",
                "exchange_order_id": updated.get("exchange_order_id")}

    async def _on_submit_error(self, row: Dict[str, Any], exc: Exception) -> Dict[str, Any]:
        outcome = getattr(exc, "outcome", "unknown")
        message = str(exc)
        if outcome in ("rejected", "not_sent"):
            await ops_alerts.enqueue(
                "order_rejected" if outcome == "rejected" else "order_not_sent",
                f"{row['venue']} {row['instrument']} order {row['id']}: {message[:300]}",
                severity="warning", dedupe_key=f"{outcome}:{row['venue']}:{message[:80]}", dedupe_seconds=600)
        if outcome == "rejected":
            await order_store.transition(row["id"], order_state.REJECTED, expected=order_state.PENDING_SUBMIT,
                                         event="rejected", detail={"error": message}, last_error=message[:500])
            if isinstance(exc, ExchangeHTTPError) and exc.is_auth:
                raise _err(502, "venue_auth_failed", f"{row['venue']} rejected the API credentials: {message}",
                           order_id=row["id"])
            raise _err(422, "exchange_rejected", f"{row['venue']} rejected the order: {message}", order_id=row["id"])
        if outcome == "not_sent":
            await order_store.transition(row["id"], order_state.REJECTED, expected=order_state.PENDING_SUBMIT,
                                         event="not_sent", detail={"error": message}, last_error=message[:500])
            raise _err(503, "venue_unreachable", f"order was not sent: {message}", order_id=row["id"])
        return await self._mark_unknown(row, message)

    async def _mark_unknown(self, row: Dict[str, Any], message: str) -> Dict[str, Any]:
        """Outcome unknown: record it, then ask the venue about our client order id right away."""
        updated = await order_store.transition(
            row["id"], order_state.UNKNOWN, expected=order_state.PENDING_SUBMIT,
            event="outcome_unknown", detail={"error": message}, last_error=message[:500])
        await ops_alerts.enqueue(
            "order_outcome_unknown",
            f"{row['venue']} {row['instrument']} order {row['id']}: exchange did not answer ({message[:200]}). "
            "It may exist on the exchange; do not resend.",
            dedupe_key=f"unknown:{row['id']}", dedupe_seconds=3600)
        for attempt in range(LOOKUP_ATTEMPTS):
            await asyncio.sleep(LOOKUP_DELAY * (attempt + 1))
            try:
                found = await self.reconcile_order(updated or await order_store.get_order(row["id"]))
            except Exception:  # noqa: BLE001
                found = None
            current = await order_store.get_order(row["id"])
            if current and current["status"] != order_state.UNKNOWN:
                return {"success": True, "order": current, "mode": "live", "reconciled": True,
                        "exchange_order_id": current.get("exchange_order_id")}
        current = await order_store.get_order(row["id"])
        raise HTTPException(status_code=202, detail={
            "error": "outcome_unknown",
            "message": "The exchange did not answer in time. The order may or may not exist; "
                       "it is being reconciled by client order id. Do NOT resubmit.",
            "order": current,
        })

    # ═════════════════════════════════════════════════════════════════════════
    # Cancel
    # ═════════════════════════════════════════════════════════════════════════

    async def cancel(self, *, user: str, order_id: str) -> Dict[str, Any]:
        row = await order_store.get_order(order_id)
        if row is None:
            raise _err(404, "not_found", f"Order {order_id} not found or already closed")
        if row.get("mode") != "live":
            cancelled = await database.cancel_order(order_id)
            if not cancelled:
                raise _err(404, "not_found", f"Order {order_id} not found or already closed")
            await database.log_action(action="order_cancelled", user_id=user, resource=f"order:{order_id}")
            return {"success": True, "message": f"Order {order_id} cancelled"}

        status = order_state.normalize(row["status"])
        if order_state.is_terminal(status):
            raise _err(409, "already_closed", f"Order {order_id} is already {status}")

        if status in (order_state.PENDING_SUBMIT, order_state.UNKNOWN):
            # The venue may or may not know this order: find out before trying to cancel anything.
            try:
                await self.reconcile_order(row)
            except Exception:  # noqa: BLE001
                pass
            row = await order_store.get_order(order_id)
            status = order_state.normalize(row["status"])
            if order_state.is_terminal(status):
                raise _err(409, "already_closed", f"Order {order_id} is already {status}")
            if status in (order_state.PENDING_SUBMIT, order_state.UNKNOWN):
                raise _err(409, "outcome_unknown",
                           "The order's state on the exchange is still unknown; retry cancelling shortly.")

        await order_store.update_fields(order_id, cancel_requested_at=datetime.now(timezone.utc).isoformat())
        try:
            await self.manager.cancel_venue_order(
                row["venue"], row["instrument"], client_order_id=row["client_order_id"],
                order_id=row.get("exchange_order_id") or None)
        except (ExchangeHTTPError, ExchangeTransportError, ExchangeRateLimited) as exc:
            if isinstance(exc, ExchangeHTTPError) and exc.outcome == "rejected":
                # Unknown-order / already-final style answers: learn the real state, do not guess.
                await self.reconcile_order(await order_store.get_order(order_id))
                current = await order_store.get_order(order_id)
                if order_state.is_terminal(current["status"]):
                    await database.log_action(action="order_cancel_attempt", user_id=user,
                                              resource=f"order:{order_id}", details=f"final={current['status']}")
                    return {"success": True, "order": current, "message": f"Order is {current['status']}"}
                raise _err(422, "cancel_rejected", f"{row['venue']} refused the cancel: {exc}", order_id=order_id)
            await order_store.add_event(order_id, "cancel_unknown", {"error": str(exc)})
            raise HTTPException(status_code=202, detail={
                "error": "cancel_unknown",
                "message": "Cancel outcome unknown; the order is being reconciled.", "order_id": order_id})

        # Learn the venue's final answer rather than assuming the cancel succeeded
        await self.reconcile_order(await order_store.get_order(order_id))
        current = await order_store.get_order(order_id)
        await database.log_action(action="order_cancelled", user_id=user, resource=f"order:{order_id}",
                                  details=f"status={current['status']}")
        return {"success": True, "order": current,
                "message": f"Order {order_id} {current['status'].lower()}"}

    # ═════════════════════════════════════════════════════════════════════════
    # Reconcile
    # ═════════════════════════════════════════════════════════════════════════

    async def reconcile_order(self, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Ask the venue about this order (by client order id) and apply the answer. Never guesses."""
        if not row or order_state.is_terminal(row["status"]) or row.get("mode") != "live":
            return row
        venue, symbol, cid = row.get("venue"), row["instrument"], row.get("client_order_id")
        if not venue or not cid:
            return row
        try:
            vo = await self.manager.fetch_order(venue, symbol, cid)
        except RuntimeError:
            return row  # venue not connected right now: try again next round
        except (ExchangeError, ExchangeRateLimited) as exc:
            logger.info("reconcile %s: venue answered with an error (%s)", row["id"], exc)
            return row

        if vo is None:
            return await self._on_not_found(row)

        new_status = order_state.from_venue_status(venue, vo.raw_status)
        if new_status is None:
            await order_store.add_event(row["id"], "unrecognised_status", {"raw": vo.raw_status})
            logger.warning("order %s: unrecognised venue status %r", row["id"], vo.raw_status)
            return row
        try:
            return await order_store.transition(
                row["id"], new_status, event="reconciled",
                detail={"raw_status": vo.raw_status, "executed": str(vo.executed_qty)},
                exchange_order_id=vo.venue_order_id or None, filled_qty=float(vo.executed_qty),
                avg_price=float(vo.avg_price) if vo.avg_price else None, check_count=0) or row
        except order_state.InvalidTransition:
            await order_store.add_event(row["id"], "stale_venue_status", {"raw": vo.raw_status})
            return row

    async def _on_not_found(self, row: Dict[str, Any]) -> Dict[str, Any]:
        checks = int(row.get("check_count") or 0) + 1
        await order_store.update_fields(row["id"], check_count=checks)
        status = order_state.normalize(row["status"])
        if (status in (order_state.PENDING_SUBMIT, order_state.UNKNOWN)
                and checks >= NOT_FOUND_MIN_CHECKS and _age_secs(row) >= NOT_FOUND_GRACE_SECS):
            msg = f"not found on {row['venue']} after {checks} checks over {int(_age_secs(row))}s"
            updated = await order_store.transition(
                row["id"], order_state.REJECTED, expected=status, event="not_found",
                detail={"checks": checks}, last_error=msg)
            return updated or row
        if status in (order_state.SUBMITTED, order_state.PARTIALLY_FILLED):
            if checks in (1, 10, 60, 360):   # do not write an event every round forever
                await order_store.add_event(row["id"], "missing_on_venue", {"checks": checks})
                logger.error("order %s (%s) is not found on the venue (%d checks)", row["id"], status, checks)
        return row

    async def reconcile_all(self) -> int:
        """One reconciliation round over every non-terminal live order. Returns orders examined."""
        reverify = getattr(self.manager, "reverify_offline", None)
        if reverify:
            try:
                await reverify()          # venues that were unreachable at startup get another chance
            except Exception:  # noqa: BLE001
                logger.exception("re-verifying offline venues failed")
        orders = await order_store.list_open_live()
        cancel_pending = (await trading_control.get_state())["cancel_pending"]
        for row in orders:
            try:
                updated = await self.reconcile_order(row)
                if (cancel_pending and updated and order_state.normalize(updated["status"])
                        in (order_state.SUBMITTED, order_state.PARTIALLY_FILLED)):
                    await self._kill_cancel(updated)   # discovered on the venue after the kill: cancel it
            except Exception:  # noqa: BLE001 - one bad order must not stop the round
                logger.exception("reconcile failed for %s", row.get("id"))
            await asyncio.sleep(0.05)  # stay well inside venue rate limits
        await self._alert_stuck(orders)
        return len(orders)

    async def _alert_stuck(self, orders: List[Dict[str, Any]]) -> None:
        """Page when a live order stays UNKNOWN / not final for too long (needs a human to check the venue)."""
        now = datetime.now(timezone.utc)
        for row in orders:
            try:
                age = (now - datetime.fromisoformat(row["timestamp"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                continue
            current = await order_store.get_order(row["id"])
            if current and current["status"] == order_state.UNKNOWN and age >= STUCK_UNKNOWN_SECONDS:
                await ops_alerts.enqueue(
                    "order_stuck_unknown",
                    f"{row['venue']} {row['instrument']} order {row['id']} has been UNKNOWN for {int(age)}s. "
                    "Check the exchange manually.",
                    dedupe_key=f"stuck:{row['id']}", dedupe_seconds=1800)

    async def _kill_cancel(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Best-effort cancel of one order because the kill switch was pulled; learns the final state."""
        try:
            await order_store.update_fields(row["id"], cancel_requested_at=datetime.now(timezone.utc).isoformat())
            await self.manager.cancel_venue_order(
                row["venue"], row["instrument"], client_order_id=row["client_order_id"],
                order_id=row.get("exchange_order_id") or None)
        except (ExchangeError, ExchangeRateLimited, RuntimeError) as exc:
            await order_store.add_event(row["id"], "kill_cancel_failed", {"error": str(exc)})
            return None
        return await self.reconcile_order(await order_store.get_order(row["id"]))

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self.reconcile_all()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("reconcile round failed")
            await asyncio.sleep(RECONCILE_INTERVAL)

    def start(self) -> None:
        """Start the background reconciler (disable with ORDER_RECONCILER_ENABLED=0)."""
        if os.getenv("ORDER_RECONCILER_ENABLED", "1") != "1":
            return
        loop = asyncio.get_running_loop()
        task = self._reconcile_task
        if task is not None and (task.done() or task.get_loop() is not loop):
            task = None  # finished, or left over from another (closed) event loop
        if task is None:
            self._reconcile_task = loop.create_task(self._reconcile_loop())

    async def stop(self) -> None:
        task, self._reconcile_task = self._reconcile_task, None
        if task is None:
            return
        task.cancel()
        if task.get_loop() is asyncio.get_running_loop():
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ═════════════════════════════════════════════════════════════════════════
    # Kill switch
    # ═════════════════════════════════════════════════════════════════════════

    async def kill(self, *, user: str, reason: str, cancel_open: bool = True) -> Dict[str, Any]:
        """
        Disarm first (no new live orders from this instant), then cancel what is open.

        Orders whose state is still unknown (being submitted / timed out) cannot be cancelled yet; the
        kill intent is remembered (`cancel_pending`), so the reconciler cancels them the moment they
        show up on the venue. The response says exactly what is still unresolved.
        """
        state = await trading_control.disarm(user, reason, cancel_open=cancel_open)
        open_orders = await order_store.list_open_live() if cancel_open else []
        for row in open_orders:
            await order_store.update_fields(row["id"], cancel_requested_at=datetime.now(timezone.utc).isoformat())

        sem = asyncio.Semaphore(4)

        async def one(row: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                try:
                    r = await self.cancel(user=user, order_id=row["id"])
                    status = (r.get("order") or {}).get("status") or order_state.CANCELLED
                    return {"order_id": row["id"], "status": order_state.normalize(status),
                            "result": "cancelled" if order_state.normalize(status) == order_state.CANCELLED else "closed"}
                except HTTPException as exc:
                    detail = exc.detail.get("error") if isinstance(exc.detail, dict) else str(exc.detail)
                    return {"order_id": row["id"], "status": order_state.normalize(row["status"]),
                            "result": "pending", "detail": detail}
                except Exception as exc:  # noqa: BLE001
                    return {"order_id": row["id"], "status": order_state.normalize(row["status"]),
                            "result": "error", "detail": str(exc)}

        results = list(await asyncio.gather(*(one(r) for r in open_orders)))
        unresolved = [r for r in results if r["result"] in ("pending", "error")]
        await database.log_action(
            action="kill_switch", user_id=user, resource="trading",
            details=f"reason={reason} orders={len(results)} unresolved={len(unresolved)}")
        return {"state": state, "cancel_results": results, "unresolved_orders": len(unresolved),
                "all_clear": not unresolved}


# Module-level singleton wired to the shared live manager
from state import live_manager  # noqa: E402

order_service = OrderService(live_manager)
