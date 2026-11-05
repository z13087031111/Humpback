"""Order state machine.

Core rule: when the outcome of a submit or cancel is not known, the order is
``UNKNOWN_IN_FLIGHT``. It keeps counting as live exposure until the venue tells us what
happened. "No answer" is never read as "no order".

Late and duplicate reports are expected (fills after a cancel, an ACK after a fill). They are
applied when they add information and ignored when they would move an order backwards.
Contradictions (a reject after a fill, an overfill) raise ``InvalidTransition``.

Two facts are tracked separately from the status, because a status alone loses them:

* ``acknowledged`` – the venue has proven the order exists (ACK, fill, definite status). A
  cancel sent for an unacknowledged order does not make it any less unknown, and an
  acknowledged order can never be written off as "not found".
* ``venue_filled_qty`` – how much the venue says has filled. When it runs ahead of the fills
  we have booked, the difference still counts as exposure (``unbooked_qty``).
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

_UNPROVEN = frozenset({OrderStatus.SUBMITTING, OrderStatus.UNKNOWN_IN_FLIGHT})


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
    acknowledged: bool = False
    venue_filled_qty: Decimal = ZERO

    @property
    def remaining(self) -> Decimal:
        return self.intent.quantity - self.filled_qty

    @property
    def needs_resolution(self) -> bool:
        """The venue has not yet proven what happened to this order."""
        if self.status in _UNPROVEN:
            return True
        return self.status is OrderStatus.CANCEL_REQUESTED and not self.acknowledged

    @property
    def unbooked_qty(self) -> Decimal:
        """Quantity the venue reports as filled that has no booked fill yet."""
        return max(self.venue_filled_qty - self.filled_qty, ZERO)

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
    return OrderStatus.PARTIALLY_FILLED  # from UNKNOWN too: a fill proves the venue accepted it


def _decimal(payload: dict[str, Any], key: str) -> Decimal:
    value = payload.get(key)
    return ZERO if value in (None, "") else Decimal(str(value))


def _venue_filled(view: OrderView, payload: dict[str, Any]) -> Decimal:
    reported = _decimal(payload, "filled_qty")
    if reported > view.intent.quantity:
        raise InvalidTransition(f"venue reports overfill on {view.intent.intent_id}")
    return max(view.venue_filled_qty, reported)


def apply_fill(view: OrderView, fill: Fill, at_ms: int) -> OrderView:
    if view.status is OrderStatus.NOT_FOUND_AT_VENUE:
        raise InvalidTransition(f"fill for {view.intent.intent_id} after not-found confirmation")
    if view.status in (OrderStatus.CREATED, OrderStatus.RISK_REJECTED, OrderStatus.REJECTED):
        raise _bad(view, "fill")
    intent = view.intent
    if (
        fill.side is not intent.side
        or fill.symbol != intent.symbol
        or fill.venue is not intent.venue
        or fill.account != intent.account
    ):
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
        acknowledged=True,
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
        venue_filled = _venue_filled(view, payload)
        if s in _UNPROVEN:
            status = OrderStatus.PARTIALLY_FILLED if view.filled_qty > ZERO else OrderStatus.OPEN
            return replace(
                view,
                status=status,
                venue_order_id=venue_order_id,
                updated_at_ms=at_ms,
                unknown_since_ms=None,
                acknowledged=True,
                venue_filled_qty=venue_filled,
            )
        if s in LIVE or s in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            # late ACK: no status change, but it does prove the order exists
            return replace(
                view,
                venue_order_id=venue_order_id,
                acknowledged=True,
                venue_filled_qty=venue_filled,
            )
        raise _bad(view, kind)

    if kind is EventKind.VENUE_REJECTED:
        if view.filled_qty > ZERO:
            raise InvalidTransition(f"reject after fill on {view.intent.intent_id}")
        if view.acknowledged:
            raise InvalidTransition(f"reject after acknowledgement on {view.intent.intent_id}")
        if s in _UNPROVEN:
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

    if kind in (EventKind.CANCEL_CONFIRMED, EventKind.ORDER_EXPIRED):
        target = OrderStatus.CANCELED if kind is EventKind.CANCEL_CONFIRMED else OrderStatus.EXPIRED
        venue_filled = _venue_filled(view, payload)
        if s in LIVE and not (kind is EventKind.CANCEL_CONFIRMED and s is OrderStatus.SUBMITTING):
            status = OrderStatus.FILLED if view.filled_qty == view.intent.quantity else target
            return replace(
                view,
                status=status,
                updated_at_ms=at_ms,
                unknown_since_ms=None,
                acknowledged=True,
                venue_filled_qty=venue_filled,
            )
        if s in TERMINAL:
            return replace(view, venue_filled_qty=venue_filled)
        raise _bad(view, kind)

    if kind is EventKind.FILL_RECORDED:
        return apply_fill(view, Fill.from_record(payload["fill"]), at_ms)

    if kind is EventKind.VENUE_STATUS_OBSERVED:
        return _apply_observed(view, payload, at_ms)

    if kind is EventKind.NOT_FOUND_CONFIRMED:
        if not view.needs_resolution:
            raise _bad(view, kind)
        if (
            view.acknowledged
            or view.venue_order_id is not None
            or view.filled_qty > ZERO
            or view.venue_filled_qty > ZERO
        ):
            raise InvalidTransition(
                f"order {view.intent.intent_id} was acknowledged or filled; it cannot be absent"
            )
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
        if view.needs_resolution:
            return replace(view, not_found_checks=view.not_found_checks + 1, updated_at_ms=at_ms)
        return view
    target = _OBSERVED.get(observed)
    if target is None:
        raise InvalidTransition(f"unknown observed status {observed!r}")
    venue_filled = _venue_filled(view, payload)
    if view.status in TERMINAL:
        if target is view.status or view.status is OrderStatus.FILLED:
            return replace(view, venue_filled_qty=venue_filled)
        if target is OrderStatus.FILLED and view.status in (
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
        ):
            # Venue says filled, we said cancelled: wait for the fill events to arrive.
            # Meanwhile the unbooked quantity keeps counting as exposure.
            return replace(view, venue_filled_qty=max(venue_filled, view.intent.quantity))
        raise InvalidTransition(
            f"venue reports {observed} but ledger has {view.status} for {view.intent.intent_id}"
        )
    if target is OrderStatus.REJECTED and (view.filled_qty > ZERO or view.acknowledged):
        raise InvalidTransition(
            f"venue reports rejected but the order was accepted: {view.intent.intent_id}"
        )
    if target is OrderStatus.REJECTED:
        return replace(view, status=target, updated_at_ms=at_ms, unknown_since_ms=None)
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
            acknowledged=True,
            venue_filled_qty=max(venue_filled, view.intent.quantity),
        )
    if target is OrderStatus.OPEN and view.filled_qty > ZERO:
        target = OrderStatus.PARTIALLY_FILLED
    if target is OrderStatus.OPEN and view.status is OrderStatus.CANCEL_REQUESTED:
        target = OrderStatus.CANCEL_REQUESTED
    if target in (OrderStatus.CANCELED, OrderStatus.EXPIRED) and (
        view.filled_qty == view.intent.quantity
    ):
        target = OrderStatus.FILLED
    return replace(
        view,
        status=target,
        venue_order_id=venue_order_id,
        updated_at_ms=at_ms,
        unknown_since_ms=None,
        not_found_checks=0,
        acknowledged=True,
        venue_filled_qty=venue_filled,
    )
