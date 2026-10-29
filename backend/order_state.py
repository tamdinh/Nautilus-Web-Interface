"""
Order lifecycle state machine (pure logic, no I/O).

Live order flow:

    PENDING_SUBMIT ──▶ SUBMITTED ──▶ PARTIALLY_FILLED ──▶ FILLED
         │                │  │               │
         │                │  └──▶ CANCELLED ◀┘
         ├──▶ REJECTED ◀──┘
         └──▶ UNKNOWN ──▶ (any of the above once the venue answers)

PENDING_SUBMIT is persisted BEFORE anything is sent, so a crash between "sent" and "recorded"
leaves a row that the reconciler can resolve by client_order_id. UNKNOWN means the request may
or may not have reached the venue (timeout, 5xx, dropped connection) and must be resolved by
querying the venue — never assumed rejected and never blindly retried.

Paper orders keep the legacy PENDING status (nothing ever fills them).
"""

from typing import Optional

# Lifecycle statuses (stored upper-case everywhere)
PENDING = "PENDING"                    # paper order, never sent anywhere
PENDING_SUBMIT = "PENDING_SUBMIT"      # live order persisted, not yet sent
SUBMITTED = "SUBMITTED"                # accepted by the venue, resting / awaiting fills
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"                    # outcome of the submit request is not known

TERMINAL = frozenset({FILLED, CANCELLED, REJECTED})
# Live orders the reconciler must keep following until they reach a terminal state
OPEN_LIVE = frozenset({PENDING_SUBMIT, SUBMITTED, PARTIALLY_FILLED, UNKNOWN})
ALL_STATUSES = TERMINAL | OPEN_LIVE | {PENDING}

_ALLOWED = {
    PENDING: {CANCELLED, FILLED},
    PENDING_SUBMIT: {SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, UNKNOWN},
    UNKNOWN: {SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED},
    SUBMITTED: {PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED},
    PARTIALLY_FILLED: {FILLED, CANCELLED},
    FILLED: set(),
    CANCELLED: set(),
    REJECTED: set(),
}


class InvalidTransition(ValueError):
    """Raised when a status change is not allowed by the lifecycle."""


def normalize(status: Optional[str]) -> str:
    """Upper-case a stored status; legacy lower-case values ('filled', 'pending') are accepted."""
    s = (status or "").strip().upper()
    if s == "CANCELED":
        return CANCELLED
    return s if s in ALL_STATUSES else s


def is_terminal(status: Optional[str]) -> bool:
    return normalize(status) in TERMINAL


def can_transition(current: Optional[str], new: Optional[str]) -> bool:
    cur, nxt = normalize(current), normalize(new)
    if cur == nxt:
        return True  # idempotent update (e.g. fill quantity changed, status did not)
    return nxt in _ALLOWED.get(cur, set())


def check_transition(current: Optional[str], new: Optional[str]) -> None:
    if not can_transition(current, new):
        raise InvalidTransition(f"order status {normalize(current)} -> {normalize(new)} is not allowed")


# ── Venue status mapping ──────────────────────────────────────────────────────

_BINANCE = {
    "NEW": SUBMITTED,
    "PENDING_NEW": SUBMITTED,
    "PARTIALLY_FILLED": PARTIALLY_FILLED,
    "FILLED": FILLED,
    "CANCELED": CANCELLED,
    "CANCELLED": CANCELLED,
    "EXPIRED": CANCELLED,
    "EXPIRED_IN_MATCH": CANCELLED,
    "REJECTED": REJECTED,
    # PENDING_CANCEL: the order is still live until the cancel completes -> keep SUBMITTED
    "PENDING_CANCEL": SUBMITTED,
}

_BYBIT = {
    "NEW": SUBMITTED,
    "CREATED": SUBMITTED,
    "UNTRIGGERED": SUBMITTED,
    "TRIGGERED": SUBMITTED,
    "PARTIALLYFILLED": PARTIALLY_FILLED,
    "FILLED": FILLED,
    "CANCELLED": CANCELLED,
    "PARTIALLYFILLEDCANCELED": CANCELLED,
    "DEACTIVATED": CANCELLED,
    "REJECTED": REJECTED,
}


def from_venue_status(venue: str, raw: Optional[str]) -> Optional[str]:
    """Map a venue's raw order status to ours; None if unrecognised (caller must not guess)."""
    key = (raw or "").strip().upper()
    if venue.upper() == "BINANCE":
        return _BINANCE.get(key)
    if venue.upper() == "BYBIT":
        return _BYBIT.get(key.replace("_", ""))
    return None
