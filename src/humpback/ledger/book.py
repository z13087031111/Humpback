"""The ledger: event store plus in-memory projections rebuilt by replay.

Four books, all derived from the same events:

* orders     – one ``OrderView`` per intent (see ``orders.py``);
* fills      – every venue fill exactly once (deduplicated by venue fill id);
* positions  – signed quantity, average entry, realized PnL, per venue/account/symbol;
* collateral – deposits, withdrawals, realized PnL, fees and funding, per venue/account,
  plus capital locked in cross-venue transfers (``in_flight_locked``).

Every mutating method validates the transition against the current view *before* appending,
so an invalid event never reaches the store.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from humpback.core.errors import InvalidTransition, LedgerError
from humpback.core.ids import client_key
from humpback.core.money import ZERO, dec, to_wire
from humpback.domain.models import Fill, OrderIntent, Venue
from humpback.ledger import orders as om
from humpback.ledger.events import EventKind, account_stream, order_stream
from humpback.ledger.orders import OrderStatus, OrderView
from humpback.ledger.store import EventStore, StoredEvent

PositionKey = tuple[Venue, str, str]  # venue, account, symbol
AccountKey = tuple[Venue, str]


@dataclass(frozen=True, slots=True)
class Position:
    quantity: Decimal = ZERO  # signed
    avg_entry: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees: Decimal = ZERO

    def apply_fill(self, fill: Fill) -> tuple[Position, Decimal]:
        """Average-cost accounting. Returns the new position and the PnL realized by the fill."""
        signed = fill.side.sign * fill.quantity
        qty, avg = self.quantity, self.avg_entry
        realized = ZERO
        if qty == ZERO or (qty > ZERO) == (signed > ZERO):
            new_qty = qty + signed
            new_avg = (abs(qty) * avg + fill.quantity * fill.price) / abs(new_qty)
        else:
            closing = min(abs(signed), abs(qty))
            direction = Decimal(1) if qty > ZERO else Decimal(-1)
            realized = closing * (fill.price - avg) * direction
            new_qty = qty + signed
            if new_qty == ZERO:
                new_avg = ZERO
            elif (new_qty > ZERO) == (qty > ZERO):
                new_avg = avg
            else:
                new_avg = fill.price  # flipped: the excess opens a new position at fill price
        return (
            Position(new_qty, new_avg, self.realized_pnl + realized, self.fees + fill.fee),
            realized,
        )


@dataclass(frozen=True, slots=True)
class Collateral:
    total: Decimal = ZERO
    in_flight_locked: Decimal = ZERO

    @property
    def free(self) -> Decimal:
        return self.total - self.in_flight_locked


class Ledger:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self._orders: dict[str, OrderView] = {}
        self._by_business_key: dict[str, str] = {}
        self._by_client_id: dict[str, str] = {}
        self._positions: dict[PositionKey, Position] = {}
        self._collateral: dict[AccountKey, Collateral] = {}
        self._realized_by_day: dict[tuple[AccountKey, int], Decimal] = {}
        self._last_seq = 0
        for event in store.events():
            self._project(event)

    # ------------------------------------------------------------------ queries

    def order(self, intent_id: str) -> OrderView:
        try:
            return self._orders[intent_id]
        except KeyError:
            raise LedgerError(f"unknown intent {intent_id}") from None

    def order_by_client_id(self, client_id: str) -> OrderView | None:
        intent_id = self._by_client_id.get(client_id)
        return None if intent_id is None else self._orders[intent_id]

    def orders(self) -> list[OrderView]:
        return list(self._orders.values())

    def live_orders(self, venue: Venue | None = None) -> list[OrderView]:
        return [
            o
            for o in self._orders.values()
            if o.is_live and (venue is None or o.intent.venue is venue)
        ]

    def unresolved_orders(self) -> list[OrderView]:
        return [o for o in self._orders.values() if o.needs_resolution]

    def exposure_orders(self, venue: Venue | None = None) -> list[OrderView]:
        """Orders that can still change a position: live ones, plus finished ones whose
        venue-reported fills have not all been booked yet."""
        return [
            o
            for o in self._orders.values()
            if (o.is_live or o.unbooked_qty > ZERO) and (venue is None or o.intent.venue is venue)
        ]

    def position(self, venue: Venue, account: str, symbol: str) -> Position:
        return self._positions.get((venue, account, symbol), Position())

    def positions(self) -> dict[PositionKey, Position]:
        return dict(self._positions)

    def collateral(self, venue: Venue, account: str) -> Collateral:
        return self._collateral.get((venue, account), Collateral())

    def realized_on_day(self, venue: Venue, account: str, day: int) -> Decimal:
        """Realized PnL net of fees and funding on one UTC day."""
        return self._realized_by_day.get(((venue, account), day), ZERO)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    # ------------------------------------------------------------------ order commands

    def record_intent(
        self, intent: OrderIntent, *, client_id: str, at_ms: int
    ) -> tuple[OrderView, bool]:
        """Record a decision. Returns ``(view, created)``.

        Re-recording the same business key returns the existing view (``created=False``);
        the same key with different content raises.
        """
        existing_id = self._by_business_key.get(intent.business_key)
        if existing_id is None and intent.intent_id in self._orders:
            raise LedgerError(f"intent id {intent.intent_id} already used by another decision")
        if existing_id is not None:
            existing = self._orders[existing_id]
            if existing.intent.content_hash() != intent.content_hash():
                raise LedgerError(f"business key {intent.business_key!r} reused with new content")
            return existing, False
        if client_id in self._by_client_id:
            raise LedgerError(f"client id collision {client_id}")
        payload = {"intent": intent.to_record(), "client_id": client_id}
        event = self.store.append(
            EventKind.INTENT_RECORDED,
            order_stream(intent.intent_id),
            payload,
            at_ms=at_ms,
            trace_id=intent.trace_id,
            dedupe_key=f"bk:{intent.business_key}",
        )
        if event is None:  # pragma: no cover - guarded by the in-memory index above
            raise LedgerError("business key already recorded")
        self._project(event)
        return self._orders[intent.intent_id], True

    def transition(
        self,
        intent_id: str,
        kind: EventKind,
        payload: dict[str, Any] | None = None,
        *,
        at_ms: int,
        trace_id: str | None = None,
    ) -> OrderView:
        view = self.order(intent_id)
        body = payload or {}
        candidate = om.apply(view, kind, body, at_ms)  # validate first; raises on contradiction
        if candidate == view and kind in (
            EventKind.VENUE_ACCEPTED,
            EventKind.CANCEL_CONFIRMED,
            EventKind.ORDER_EXPIRED,
            EventKind.OUTCOME_UNKNOWN,
            EventKind.CANCEL_REQUESTED,
        ):
            return view  # a duplicate or late report that adds nothing
        event = self.store.append(
            kind,
            order_stream(intent_id),
            body,
            at_ms=at_ms,
            trace_id=trace_id or view.intent.trace_id,
        )
        assert event is not None
        self._project(event)
        return self._orders[intent_id]

    def record_fill(self, fill: Fill, *, at_ms: int, trace_id: str | None = None) -> bool:
        """Append a fill once. Returns False for a duplicate report of the same venue fill."""
        dedupe_key = f"fill:{fill.venue.value}:{fill.venue_fill_id}"
        if self.store.dedupe_seq(dedupe_key) is not None:
            return False
        view = self.order(fill.intent_id)
        om.apply_fill(view, fill, at_ms)  # validate before appending
        event = self.store.append(
            EventKind.FILL_RECORDED,
            order_stream(fill.intent_id),
            {"fill": fill.to_record()},
            at_ms=at_ms,
            trace_id=trace_id or view.intent.trace_id,
            dedupe_key=dedupe_key,
        )
        if event is None:
            return False
        self._project(event)
        return True

    # ------------------------------------------------------------------ account commands

    def adjust_collateral(
        self,
        venue: Venue,
        account: str,
        delta: Decimal,
        *,
        reason: str,
        reference: str,
        at_ms: int,
        trace_id: str,
    ) -> bool:
        """Deposits, withdrawals and manual corrections. ``reference`` makes it idempotent."""
        event = self.store.append(
            EventKind.COLLATERAL_ADJUSTED,
            account_stream(venue.value, account),
            {
                "venue": venue.value,
                "account": account,
                "delta": to_wire(dec(delta)),
                "reason": reason,
                "reference": reference,
            },
            at_ms=at_ms,
            trace_id=trace_id,
            dedupe_key=f"collateral:{venue.value}:{reference}",
        )
        if event is None:
            return False
        self._project(event)
        return True

    def record_funding(
        self,
        venue: Venue,
        account: str,
        symbol: str,
        amount: Decimal,
        *,
        venue_event_id: str,
        at_ms: int,
        trace_id: str,
    ) -> bool:
        """Funding paid (negative) or received (positive). Deduplicated by venue event id."""
        event = self.store.append(
            EventKind.FUNDING_RECORDED,
            account_stream(venue.value, account),
            {
                "venue": venue.value,
                "account": account,
                "symbol": symbol,
                "amount": to_wire(dec(amount)),
                "venue_event_id": venue_event_id,
            },
            at_ms=at_ms,
            trace_id=trace_id,
            dedupe_key=f"funding:{venue.value}:{venue_event_id}",
        )
        if event is None:
            return False
        self._project(event)
        return True

    def append_raw(
        self,
        kind: EventKind,
        stream: str,
        payload: dict[str, Any],
        *,
        at_ms: int,
        trace_id: str,
        dedupe_key: str | None = None,
    ) -> StoredEvent | None:
        """For other modules (capital, halt) that keep their own streams in the same store."""
        event = self.store.append(
            kind, stream, payload, at_ms=at_ms, trace_id=trace_id, dedupe_key=dedupe_key
        )
        if event is not None:
            self._project(event)
        return event

    def events_for(self, stream: str) -> list[StoredEvent]:
        return [e for e in self.store.events() if e.stream == stream]

    # ------------------------------------------------------------------ projection

    def _project(self, event: StoredEvent) -> None:
        kind = EventKind(event.kind)
        payload = event.payload
        if kind is EventKind.INTENT_RECORDED:
            intent = OrderIntent.from_record(payload["intent"])
            view = OrderView(
                intent=intent, client_id=payload["client_id"], updated_at_ms=event.at_ms
            )
            self._orders[intent.intent_id] = view
            self._by_business_key[intent.business_key] = intent.intent_id
            self._by_client_id[view.client_id] = intent.intent_id
        elif kind is EventKind.FILL_RECORDED:
            fill = Fill.from_record(payload["fill"])
            view = self._orders[fill.intent_id]
            self._orders[fill.intent_id] = om.apply_fill(view, fill, event.at_ms)
            key = (fill.venue, fill.account, fill.symbol)
            position, realized = self._positions.get(key, Position()).apply_fill(fill)
            self._positions[key] = position
            self._bump_collateral((fill.venue, fill.account), realized - fill.fee, event.at_ms)
        elif kind in (EventKind.COLLATERAL_ADJUSTED,):
            account = (Venue(payload["venue"]), payload["account"])
            current = self._collateral.get(account, Collateral())
            self._collateral[account] = replace(
                current, total=current.total + Decimal(payload["delta"])
            )
        elif kind is EventKind.FUNDING_RECORDED:
            self._bump_collateral(
                (Venue(payload["venue"]), payload["account"]),
                Decimal(payload["amount"]),
                event.at_ms,
            )
        elif kind is EventKind.TRANSFER_EVENT:
            self._project_transfer(payload)
        elif kind is EventKind.HALT_STAGE:
            pass  # halt records are read directly by the emergency-stop module
        else:
            intent_id = event.stream.removeprefix("order:")
            existing = self._orders.get(intent_id)
            if existing is None:
                raise LedgerError(f"event {event.seq} for unknown order {intent_id}")
            try:
                self._orders[intent_id] = om.apply(existing, kind, payload, event.at_ms)
            except InvalidTransition as exc:  # pragma: no cover - validated before append
                raise LedgerError(f"stored event {event.seq} is inconsistent: {exc}") from exc
        self._last_seq = event.seq

    def _bump_collateral(self, account: AccountKey, delta: Decimal, at_ms: int) -> None:
        current = self._collateral.get(account, Collateral())
        self._collateral[account] = replace(current, total=current.total + delta)
        day_key = (account, at_ms // 86_400_000)
        self._realized_by_day[day_key] = self._realized_by_day.get(day_key, ZERO) + delta

    def _project_transfer(self, payload: dict[str, Any]) -> None:
        """Capital transfers move ``in_flight_locked`` and, on proven credit, ``total``."""
        effect = payload.get("effect")
        amount = Decimal(payload["amount"])
        source = (Venue(payload["source_venue"]), payload["source_account"])
        src = self._collateral.get(source, Collateral())
        if effect == "lock":
            self._collateral[source] = replace(src, in_flight_locked=src.in_flight_locked + amount)
        elif effect == "credited":
            # ``amount`` left the source; ``received`` (amount minus bridge fees) arrived.
            received = Decimal(payload["received"])
            self._collateral[source] = Collateral(src.total - amount, src.in_flight_locked - amount)
            dest = (Venue(payload["dest_venue"]), payload["dest_account"])
            dst = self._collateral.get(dest, Collateral())
            self._collateral[dest] = replace(dst, total=dst.total + received)
        elif effect == "refunded":
            # The lock is released; whatever the refund did not return is a realized loss.
            received = Decimal(payload["received"])
            self._collateral[source] = Collateral(
                src.total - (amount - received), src.in_flight_locked - amount
            )


def default_client_id(business_key: str, salt: int = 0) -> str:
    """Venue-neutral client id: ``0x`` + 32 hex chars (16 bytes)."""
    return "0x" + client_key(business_key, salt).hex()


__all__ = ["Collateral", "Ledger", "OrderStatus", "OrderView", "Position", "default_client_id"]
