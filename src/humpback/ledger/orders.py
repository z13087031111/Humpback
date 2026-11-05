"""Order state machine.

Core rule: when the outcome of a submit or cancel is not known, the order is
``UNKNOWN_IN_FLIGHT``. It keeps counting as live exposure until the venue tells us what
happened. "No answer" is never read as "no order".

Late and duplicate reports are expected (fills after a cancel, an ACK after a fill). They are
applied when they add information and ignored when they would move an order backwards.
Contradictions (a reject after a fill, an overfill) raise ``InvalidTransition``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any

from humpback.core.errors import InvalidTransition
from humpback.core.money import ZERO
from humpback.domain.models import Fill, OrderIntent
from humpback.ledger.events import EventKind


class OrderStatus(StrEnum):
    CREATED = "created"
    RISK_REJECTED = "risk_rejected"
    SUBMITTING = "submitting"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    CANCEL_REQUESTED = "cancel_requested"
    UNKNOWN_IN_FLIGHT = "unknown_in_flight"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    NOT_FOUND_AT_VENUE = "not_found_at_venue"


TERMINAL = frozenset(
    {
        OrderStatus.RISK_REJECTED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
        OrderStatus.NOT_FOUND_AT_VENUE,
    }
)

# States in which the venue may hold a live order for us. Risk counts their full
# remaining quantity as exposure; reconciliation must explain them.
LIVE = frozenset(
    {
        OrderStatus.SUBMITTING,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCEL_REQUESTED,
        OrderStatus.UNKNOWN_IN_FLIGHT,
    }
)

NEEDS_RESOLUTION = frozenset({OrderStatus.SUBMITTING, OrderStatus.UNKNOWN_IN_FLIGHT})


@dataclass(frozen=True, slots=True)
class OrderView:
    intent: OrderIntent
    client_id: str
    status: OrderStatus = OrderStatus.CREATED
    venue_order_id: str | None = None
    filled_qty: Decimal = ZERO
    filled_notional: Decimal = ZERO
    fees: Decimal = ZERO
    reason: str | None = None
    updated_at_ms: int = 0
    unknown_since_ms: int | None = None
    not_found_checks: int = 0

    @property
    def remaining(self) -> Decimal:
        return self.intent.quantity - self.filled_qty

    @property
    def is_live(self) -> bool:
        return self.status in LIVE

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def avg_fill_price(self) -> Decimal | None:
        return None if self.filled_qty == ZERO else self.filled_notional / self.filled_qty


def _bad(view: OrderView, kind: str) -> InvalidTransition:
    return InvalidTransition(f"{kind} not allowed in {view.status} for {view.intent.intent_id}")


def _after_fill_status(view: OrderView) -> OrderStatus:
    if view.filled_qty == view.intent.quantity:
        return OrderStatus.FILLED
    if view.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.CANCEL_REQUESTED):
        return view.status  # a late fill does not reopen a cancelled order
    if view.status is OrderStatus.UNKNOWN_IN_FLIGHT:
        return OrderStatus.PARTIALLY_FILLED  # a fill proves the venue accepted it
    if view.status is OrderStatus.NOT_FOUND_AT_VENUE:
        raise InvalidTransition(f"fill for {view.intent.intent_id} after not-found confirmation")
    return OrderStatus.PARTIALLY_FILLED


def apply_fill(view: OrderView, fill: Fill, at_ms: int) -> OrderView:
    if view.status in (OrderStatus.CREATED, OrderStatus.RISK_REJECTED, OrderStatus.REJECTED):
        raise _bad(view, "fill")
    if fill.side is not view.intent.side or fill.symbol != view.intent.symbol:
        raise InvalidTransition(f"fill {fill.venue_fill_id} does not match order identity")
    filled = view.filled_qty + fill.quantity
    if filled > view.intent.quantity:
        raise InvalidTransition(
            f"overfill on {view.intent.intent_id}: {filled} > {view.intent.quantity}"
        )
    updated = replace(
        view,
        filled_qty=filled,
        filled_notional=view.filled_notional + fill.notional,
        fees=view.fees + fill.fee,
        updated_at_ms=at_ms,
        unknown_since_ms=None,
    )
    return replace(updated, status=_after_fill_status(updated))


_OBSERVED = {
    "open": OrderStatus.OPEN,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
}


def apply(view: OrderView, kind: EventKind, payload: dict[str, Any], at_ms: int) -> OrderView:
    """Pure transition function. Raises ``InvalidTransition`` on contradictions."""
    s = view.status
    if kind is EventKind.RISK_REJECTED:
        if s is not OrderStatus.CREATED:
            raise _bad(view, kind)
        return replace(
            view,
            status=OrderStatus.RISK_REJECTED,
            reason=payload.get("reason"),
            updated_at_ms=at_ms,
        )

    if kind is EventKind.SUBMIT_STARTED:
        if s is not OrderStatus.CREATED:
            raise _bad(view, kind)
        return replace(view, status=OrderStatus.SUBMITTING, updated_at_ms=at_ms)

    if kind is EventKind.VENUE_ACCEPTED:
        venue_order_id = payload.get("venue_order_id") or view.venue_order_id
        if s in (OrderStatus.SUBMITTING, OrderStatus.UNKNOWN_IN_FLIGHT):
            status = OrderStatus.PARTIALLY_FILLED if view.filled_qty > ZERO else OrderStatus.OPEN
            return replace(
                view,
                status=status,
                venue_order_id=venue_order_id,
                updated_at_ms=at_ms,
                unknown_since_ms=None,
            )
        if s in LIVE or s in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            return replace(view, venue_order_id=venue_order_id)  # late ACK, no state change
        raise _bad(view, kind)

    if kind is EventKind.VENUE_REJECTED:
        if view.filled_qty > ZERO:
            raise InvalidTransition(f"reject after fill on {view.intent.intent_id}")
        if s in (OrderStatus.SUBMITTING, OrderStatus.UNKNOWN_IN_FLIGHT):
            return replace(
                view,
                status=OrderStatus.REJECTED,
                reason=payload.get("reason"),
                updated_at_ms=at_ms,
                unknown_since_ms=None,
            )
        raise _bad(view, kind)

    if kind is EventKind.OUTCOME_UNKNOWN:
        if s in (
            OrderStatus.SUBMITTING,
            OrderStatus.CANCEL_REQUESTED,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.UNKNOWN_IN_FLIGHT,
        ):
            return replace(
                view,
                status=OrderStatus.UNKNOWN_IN_FLIGHT,
                reason=payload.get("cause"),
                updated_at_ms=at_ms,
                unknown_since_ms=view.unknown_since_ms or at_ms,
                not_found_checks=0,
            )
        if s in TERMINAL:
            return view  # the order already finished; the unknown request cannot revive it
        raise _bad(view, kind)

    if kind is EventKind.CANCEL_REQUESTED:
        if s in (
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.UNKNOWN_IN_FLIGHT,
            OrderStatus.CANCEL_REQUESTED,
        ):
            return replace(view, status=OrderStatus.CANCEL_REQUESTED, updated_at_ms=at_ms)
        if s in TERMINAL:
            return view
        raise _bad(view, kind)

    if kind is EventKind.CANCEL_CONFIRMED:
        if s in LIVE and s is not OrderStatus.SUBMITTING:
            return replace(
                view, status=OrderStatus.CANCELED, updated_at_ms=at_ms, unknown_since_ms=None
            )
        if s in TERMINAL:
            return view
        raise _bad(view, kind)

    if kind is EventKind.ORDER_EXPIRED:
        if s in LIVE:
            return replace(
                view, status=OrderStatus.EXPIRED, updated_at_ms=at_ms, unknown_since_ms=None
            )
        if s in TERMINAL:
            return view
        raise _bad(view, kind)

    if kind is EventKind.FILL_RECORDED:
        return apply_fill(view, Fill.from_record(payload["fill"]), at_ms)

    if kind is EventKind.VENUE_STATUS_OBSERVED:
        return _apply_observed(view, payload, at_ms)

    if kind is EventKind.NOT_FOUND_CONFIRMED:
        if s not in NEEDS_RESOLUTION:
            raise _bad(view, kind)
        if view.filled_qty > ZERO:
            raise InvalidTransition(f"order {view.intent.intent_id} has fills; it was found")
        return replace(
            view,
            status=OrderStatus.NOT_FOUND_AT_VENUE,
            reason=payload.get("evidence"),
            updated_at_ms=at_ms,
            unknown_since_ms=None,
        )

    raise InvalidTransition(f"event {kind} does not apply to orders")


def _apply_observed(view: OrderView, payload: dict[str, Any], at_ms: int) -> OrderView:
    """A direct status query answered. Fill quantities still come only from fill events."""
    observed = payload["status"]
    venue_order_id = payload.get("venue_order_id") or view.venue_order_id
    if observed == "not_found":
        if view.status in NEEDS_RESOLUTION:
            return replace(view, not_found_checks=view.not_found_checks + 1, updated_at_ms=at_ms)
        return view
    target = _OBSERVED.get(observed)
    if target is None:
        raise InvalidTransition(f"unknown observed status {observed!r}")
    if view.status in TERMINAL:
        if target is view.status or view.status is OrderStatus.FILLED:
            return view
        if target is OrderStatus.FILLED and view.status in (
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
        ):
            # Venue says filled, we said cancelled: wait for the fill events to arrive.
            return view
        raise InvalidTransition(
            f"venue reports {observed} but ledger has {view.status} for {view.intent.intent_id}"
        )
    if target is OrderStatus.REJECTED and view.filled_qty > ZERO:
        raise InvalidTransition(
            f"venue reports rejected but fills exist on {view.intent.intent_id}"
        )
    if target is OrderStatus.FILLED and view.filled_qty < view.intent.quantity:
        # The venue says filled but we have not ingested every fill yet. Keep the order live
        # (exposure still counted) until the fills themselves arrive.
        return replace(
            view,
            status=OrderStatus.PARTIALLY_FILLED if view.filled_qty else OrderStatus.OPEN,
            venue_order_id=venue_order_id,
            updated_at_ms=at_ms,
            unknown_since_ms=None,
            not_found_checks=0,
        )
    if target is OrderStatus.OPEN and view.filled_qty > ZERO:
        target = OrderStatus.PARTIALLY_FILLED
    if target is OrderStatus.OPEN and view.status is OrderStatus.CANCEL_REQUESTED:
        target = OrderStatus.CANCEL_REQUESTED
    return replace(
        view,
        status=target,
        venue_order_id=venue_order_id,
        updated_at_ms=at_ms,
        unknown_since_ms=None,
        not_found_checks=0,
    )
