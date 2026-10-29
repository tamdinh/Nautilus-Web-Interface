"""
Pre-trade validation of order size/price against the venue's symbol filters (pure logic).

Orders are never silently rounded: a quantity or price that is not a multiple of the venue's step
is rejected with a message saying what is allowed, so the operator sees exactly what will be sent.
"""

from decimal import Decimal
from typing import Optional, Tuple

from exchange_clients import SymbolRules, _fmt


class OrderValidationError(ValueError):
    """The order violates a venue filter (it would be rejected by the exchange)."""


def _multiple_of(value: Decimal, step: Optional[Decimal]) -> bool:
    if not step or step <= 0:
        return True
    return (value % step) == 0


def validate_order(
    rules: SymbolRules,
    side: str,
    order_type: str,
    quantity: Decimal,
    price: Optional[Decimal],
    reference_price: Optional[Decimal] = None,
) -> Tuple[str, Optional[str]]:
    """
    Validate against `rules` and return the exact (qty_str, price_str) to send.
    `reference_price` (last trade price) is only used to value MARKET orders for the notional checks.
    """
    ot = order_type.upper()
    if not rules.trading:
        raise OrderValidationError(f"{rules.symbol} is not currently tradable on {rules.venue}")
    if quantity <= 0:
        raise OrderValidationError("quantity must be positive")
    if ot == "LIMIT":
        if price is None or price <= 0:
            raise OrderValidationError("limit orders require a positive price")
    elif price is not None:
        raise OrderValidationError(f"{ot} orders must not carry a price")

    # Quantity filters
    step = rules.market_step_size if (ot == "MARKET" and rules.market_step_size) else rules.step_size
    max_qty = rules.market_max_qty if (ot == "MARKET" and rules.market_max_qty) else rules.max_qty
    if rules.min_qty and quantity < rules.min_qty:
        raise OrderValidationError(f"quantity {quantity} is below the minimum {rules.min_qty} for {rules.symbol}")
    if max_qty and max_qty > 0 and quantity > max_qty:
        raise OrderValidationError(f"quantity {quantity} exceeds the maximum {max_qty} for {rules.symbol}")
    if not _multiple_of(quantity, step):
        raise OrderValidationError(f"quantity {quantity} is not a multiple of the step size {step} for {rules.symbol}")

    # Price filters
    if price is not None:
        if rules.min_price and price < rules.min_price:
            raise OrderValidationError(f"price {price} is below the minimum {rules.min_price}")
        if rules.max_price and rules.max_price > 0 and price > rules.max_price:
            raise OrderValidationError(f"price {price} is above the maximum {rules.max_price}")
        if not _multiple_of(price, rules.tick_size):
            raise OrderValidationError(f"price {price} is not a multiple of the tick size {rules.tick_size}")

    # Notional filters
    px = price if price is not None else reference_price
    applies = ot == "LIMIT" or rules.notional_applies_to_market
    if applies and (rules.min_notional or (rules.max_notional and rules.max_notional > 0)):
        if px is None:
            raise OrderValidationError("cannot value the order (no price): refusing to send")
        notional = quantity * px
        if rules.min_notional and notional < rules.min_notional:
            raise OrderValidationError(f"order value {notional} is below the minimum notional {rules.min_notional}")
        if rules.max_notional and rules.max_notional > 0 and notional > rules.max_notional:
            raise OrderValidationError(f"order value {notional} exceeds the maximum notional {rules.max_notional}")

    return format(quantity, "f"), _fmt(price)
