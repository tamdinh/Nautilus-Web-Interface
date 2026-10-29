from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

import database
import order_store
from auth_jwt import get_current_user, require_trader
from order_service import order_service
from state import nautilus_system
from utils import normalize_order

router = APIRouter(prefix="/api", tags=["orders"])

_IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9_\-:.]{8,64}$"


class OrderCreateRequest(BaseModel):
    instrument: str = Field("EUR/USD.SIM", min_length=1, max_length=50)
    side: str = Field("BUY", pattern="^(BUY|SELL)$")
    type: str = Field("MARKET", pattern="^(MARKET|LIMIT|STOP)$")
    quantity: Decimal = Field(..., gt=0, max_digits=24, decimal_places=12)
    price: Optional[Decimal] = Field(None, ge=0, max_digits=24, decimal_places=12)
    leverage: float = Field(1.0, ge=1.0, le=1000.0)
    venue: Optional[str] = Field(None, pattern="^(?i:binance|bybit)$")


_INTERNAL_FIELDS = ("request_hash", "user_id", "check_count")


def _strip_internal(order: Optional[Dict[str, Any]]) -> None:
    if order:
        for f in _INTERNAL_FIELDS:
            order.pop(f, None)


@router.get("/orders")
async def list_orders(_user: dict = Depends(get_current_user)):
    """List orders: backtest orders + persistent (paper and live) orders."""
    all_orders: List[Dict[str, Any]] = []

    for results in nautilus_system.backtest_results.values():
        for o in results.get("orders", []):
            row = normalize_order(o)
            row["timestamp"] = datetime.now(timezone.utc).isoformat()
            all_orders.append(row)

    db_orders = await database.list_orders()
    # the list is the latest 200: open live orders must never scroll out of view
    seen = {o["id"] for o in db_orders}
    db_orders += [o for o in await order_store.list_open_live() if o["id"] not in seen]
    for o in db_orders:
        _strip_internal(o)
    all_orders.extend(db_orders)
    return {"orders": all_orders, "count": len(all_orders)}


@router.get("/orders/{order_id}/events")
async def order_events(order_id: str, _user: dict = Depends(get_current_user)):
    """Lifecycle history of one order (created, submitted, reconciled, ...)."""
    if await order_store.get_order(order_id) is None:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    return {"order_id": order_id, "events": await order_store.list_events(order_id)}


@router.post("/orders")
async def create_order(
    req: OrderCreateRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key", pattern=_IDEMPOTENCY_KEY_PATTERN),
    _user: dict = Depends(require_trader),
):
    """
    Place an order. In paper mode it is recorded only; in live mode it goes to the exchange (never
    silently to paper). Send an `Idempotency-Key` header so a client retry after a timeout returns the
    same order instead of creating a second one.
    """
    import trading_control
    if not idempotency_key and (await trading_control.get_state())["mode"] == "live":
        raise HTTPException(status_code=400, detail={
            "error": "idempotency_key_required",
            "message": "Live orders require an Idempotency-Key header (one UUID per order) so a retry can never "
                       "create a second order."})
    result = await order_service.place(
        user=_user.get("sub", ""), order=req.model_dump(), idempotency_key=idempotency_key)
    _strip_internal(result.get("order"))
    return result


@router.delete("/orders/{order_id}")
async def cancel_order(order_id: str, _user: dict = Depends(require_trader)):
    return await order_service.cancel(user=_user.get("sub", ""), order_id=order_id)
