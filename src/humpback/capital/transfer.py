"""Transfer state machine.

The four recovery states are kept apart on purpose:

    TIMEOUT_DETECTED                 alert and investigate; funds stay locked
    REFUND_ELIGIBLE                  protocol rules allow a refund; funds stay locked
    REFUND_EXECUTED_PENDING_CONFIRM  refund sent (we hold a tx hash); funds stay locked
    FUNDS_ACTUALLY_CREDITED          balance increase observed on chain or at the venue

``in_flight_locked`` is released only by ``FUNDS_ACTUALLY_CREDITED`` (at the destination) or
``REFUND_CREDITED`` (back at the source), and both require ``ArrivalEvidence``. No evidence,
no release, whatever the timers say.

Bridges plug in through ``BridgeDriver``; none is hard-wired.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from humpback.core.clock import Clock
from humpback.core.errors import InvalidTransition
from humpback.core.ids import new_id
from humpback.core.money import ZERO, positive, to_wire
from humpback.domain.models import Venue
from humpback.ledger.book import Ledger
from humpback.ledger.events import EventKind


class TransferState(StrEnum):
    INITIATED = "initiated"
    SUBMITTED = "submitted"
    TIMEOUT_DETECTED = "timeout_detected"
    REFUND_ELIGIBLE = "refund_eligible"
    REFUND_EXECUTED_PENDING_CONFIRM = "refund_executed_pending_confirm"
    FUNDS_ACTUALLY_CREDITED = "funds_actually_credited"
    REFUND_CREDITED = "refund_credited"
    CANCELLED_BEFORE_SUBMIT = "cancelled_before_submit"


_ALLOWED: dict[TransferState, frozenset[TransferState]] = {
    TransferState.INITIATED: frozenset(
        {TransferState.SUBMITTED, TransferState.CANCELLED_BEFORE_SUBMIT}
    ),
    TransferState.SUBMITTED: frozenset(
        {TransferState.TIMEOUT_DETECTED, TransferState.FUNDS_ACTUALLY_CREDITED}
    ),
    TransferState.TIMEOUT_DETECTED: frozenset(
        {TransferState.REFUND_ELIGIBLE, TransferState.FUNDS_ACTUALLY_CREDITED}
    ),
    TransferState.REFUND_ELIGIBLE: frozenset(
        {TransferState.REFUND_EXECUTED_PENDING_CONFIRM, TransferState.FUNDS_ACTUALLY_CREDITED}
    ),
    TransferState.REFUND_EXECUTED_PENDING_CONFIRM: frozenset(
        {TransferState.REFUND_CREDITED, TransferState.FUNDS_ACTUALLY_CREDITED}
    ),
    TransferState.FUNDS_ACTUALLY_CREDITED: frozenset(),
    TransferState.REFUND_CREDITED: frozenset(),
    TransferState.CANCELLED_BEFORE_SUBMIT: frozenset(),
}


class EvidenceKind(StrEnum):
    CHAIN_TX_CONFIRMED = "chain_tx_confirmed"
    VENUE_BALANCE_INCREASE = "venue_balance_increase"


@dataclass(frozen=True, slots=True)
class ArrivalEvidence:
    kind: EvidenceKind
    reference: str  # tx hash, or venue ledger entry id
    received_amount: Decimal
    observed_at_ms: int
    balance_before: Decimal | None = None
    balance_after: Decimal | None = None

    def validate(self) -> None:
        positive(self.received_amount, "received_amount")
        if not self.reference:
            raise InvalidTransition("evidence needs a tx hash or venue reference")
        if self.kind is EvidenceKind.VENUE_BALANCE_INCREASE:
            if self.balance_before is None or self.balance_after is None:
                raise InvalidTransition("balance evidence needs before and after balances")
            if self.balance_after - self.balance_before < self.received_amount:
                raise InvalidTransition("observed balance increase is smaller than claimed")


class BridgeDriver(Protocol):
    """Implemented per bridge or native deposit path; none is built into phase 1."""

    def submit(self, transfer_id: str, amount: Decimal) -> str: ...

    def lookup(self, reference: str) -> ArrivalEvidence | None: ...


@dataclass(frozen=True, slots=True)
class TransferView:
    transfer_id: str
    state: TransferState
    amount: Decimal
    source: tuple[Venue, str]
    dest: tuple[Venue, str]


class TransferBook:
    def __init__(self, ledger: Ledger, clock: Clock) -> None:
        self.ledger = ledger
        self.clock = clock

    def initiate(
        self, source: tuple[Venue, str], dest: tuple[Venue, str], amount: Decimal, *, trace_id: str
    ) -> TransferView:
        positive(amount, "amount")
        free = self.ledger.collateral(*source).free
        if amount > free:
            raise InvalidTransition(f"transfer {amount} exceeds free collateral {free}")
        transfer_id = new_id()
        self._append(
            transfer_id,
            TransferState.INITIATED,
            source=source,
            dest=dest,
            amount=amount,
            effect="lock",
            trace_id=trace_id,
        )
        return self.view(transfer_id)

    def advance(
        self,
        transfer_id: str,
        state: TransferState,
        *,
        trace_id: str,
        reference: str | None = None,
        evidence: ArrivalEvidence | None = None,
    ) -> TransferView:
        current = self.view(transfer_id)
        if state not in _ALLOWED[current.state]:
            raise InvalidTransition(f"transfer {transfer_id}: {current.state} -> {state}")
        effect = "none"
        received = ZERO
        if state in (TransferState.FUNDS_ACTUALLY_CREDITED, TransferState.REFUND_CREDITED):
            if evidence is None:
                raise InvalidTransition(f"{state} requires arrival evidence")
            evidence.validate()
            if evidence.received_amount > current.amount:
                raise InvalidTransition("received more than was sent")
            effect = "credited" if state is TransferState.FUNDS_ACTUALLY_CREDITED else "refunded"
            received = evidence.received_amount
        if state is TransferState.CANCELLED_BEFORE_SUBMIT:
            effect, received = "refunded", current.amount
        if state is TransferState.REFUND_EXECUTED_PENDING_CONFIRM and not reference:
            raise InvalidTransition("a refund needs its tx hash or venue reference")
        self._append(
            transfer_id,
            state,
            source=current.source,
            dest=current.dest,
            amount=current.amount,
            effect=effect,
            trace_id=trace_id,
            reference=reference or (evidence.reference if evidence else None),
            received=received,
            # One proof of arrival can release one lock, never two.
            dedupe_key=None
            if evidence is None
            else f"evidence:{evidence.kind}:{evidence.reference}",
        )
        return self.view(transfer_id)

    def view(self, transfer_id: str) -> TransferView:
        events = self.ledger.events_for(f"transfer:{transfer_id}")
        if not events:
            raise KeyError(transfer_id)
        first, last = events[0].payload, events[-1].payload
        return TransferView(
            transfer_id,
            TransferState(last["state"]),
            Decimal(first["amount"]),
            (Venue(first["source_venue"]), first["source_account"]),
            (Venue(first["dest_venue"]), first["dest_account"]),
        )

    def _append(
        self,
        transfer_id: str,
        state: TransferState,
        *,
        source: tuple[Venue, str],
        dest: tuple[Venue, str],
        amount: Decimal,
        effect: str,
        trace_id: str,
        reference: str | None = None,
        received: Decimal = ZERO,
        dedupe_key: str | None = None,
    ) -> None:
        event = self.ledger.append_raw(
            EventKind.TRANSFER_EVENT,
            f"transfer:{transfer_id}",
            {
                "transfer_id": transfer_id,
                "state": state.value,
                "effect": effect,
                "amount": to_wire(amount),
                "received": to_wire(received),
                "source_venue": source[0].value,
                "source_account": source[1],
                "dest_venue": dest[0].value,
                "dest_account": dest[1],
                "reference": reference,
            },
            at_ms=self.clock.now_ms(),
            trace_id=trace_id,
            dedupe_key=dedupe_key,
        )
        if event is None:
            raise InvalidTransition(f"evidence already used: {dedupe_key}")
