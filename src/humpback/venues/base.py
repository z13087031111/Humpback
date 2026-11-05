"""Shared venue contracts: capability declarations, request/response types, interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol

from humpback.core.errors import UnsupportedCapability
from humpback.core.money import ZERO
from humpback.domain.models import (
    AccountSnapshot,
    Fill,
    OrderIntent,
    OrderType,
    TimeInForce,
    Venue,
)


class Action(StrEnum):
    """Every signable action. The signer whitelists a subset per phase."""

    PLACE_ORDER = "place_order"
    CANCEL_ORDER = "cancel_order"
    MODIFY_ORDER = "modify_order"
    AUTH_READ = "auth_read"
    UPDATE_LEVERAGE = "update_leverage"
    TRANSFER = "transfer"
    WITHDRAW = "withdraw"
    APPROVE_AGENT = "approve_agent"


@dataclass(frozen=True, slots=True)
class VenueCapabilities:
    venue: Venue
    order_types: frozenset[OrderType]
    time_in_force: frozenset[TimeInForce]
    reduce_only: bool
    query_by_client_id: bool
    cancel_by_client_id: bool
    client_id_format: str
    signature_scheme: str
    nonce_unit: Literal["ms", "us"]
    margin_modes: frozenset[str] = frozenset({"cross"})
    notes: tuple[str, ...] = ()

    def ensure_supported(self, intent: OrderIntent) -> None:
        """Reject locally instead of letting the venue silently change the order."""
        if intent.venue is not self.venue:
            raise UnsupportedCapability(f"intent for {intent.venue} sent to {self.venue}")
        if intent.order_type not in self.order_types:
            raise UnsupportedCapability(f"{self.venue} does not support {intent.order_type}")
        if intent.time_in_force not in self.time_in_force:
            raise UnsupportedCapability(f"{self.venue} does not support {intent.time_in_force}")
        if intent.reduce_only and not self.reduce_only:
            raise UnsupportedCapability(f"{self.venue} does not support reduce-only")


@dataclass(frozen=True, slots=True)
class UnsignedRequest:
    """A venue request ready for the signer. ``body`` field order is preserved for hashing."""

    venue: Venue
    action: Action
    account: str
    intent_id: str
    client_id: str
    intent_hash: str
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SignedRequest:
    request: UnsignedRequest
    signer_address: str
    nonce: int
    signature: str
    extra: dict[str, Any] = field(default_factory=dict)


class SubmitOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """A *definitive* venue answer. Ambiguity is signalled by raising VenueOutcomeUnknown."""

    outcome: SubmitOutcome
    venue_order_id: str | None = None
    reason: str | None = None
    terminal_status: Literal["filled", "expired", "canceled"] | None = None


ObservedStatus = Literal["open", "filled", "canceled", "rejected", "expired", "not_found"]


@dataclass(frozen=True, slots=True)
class OrderQueryResult:
    status: ObservedStatus
    venue_order_id: str | None = None
    filled_qty: Decimal = ZERO


class InstrumentRules(Protocol):
    def check(self, intent: OrderIntent, reference_price: Decimal | None) -> None:
        """Raise PrecisionError if the venue would reject the intent's numbers."""


class VenueCodec(Protocol):
    @property
    def capabilities(self) -> VenueCapabilities: ...

    def rules(self, symbol: str) -> InstrumentRules: ...

    def encode_client_id(self, key: bytes) -> str: ...

    def build_place(self, intent: OrderIntent, client_id: str) -> UnsignedRequest: ...

    def build_cancel(self, intent: OrderIntent, client_id: str) -> UnsignedRequest: ...


class VenueTransport(Protocol):
    venue: Venue

    def send(self, signed: SignedRequest) -> SubmitResult:
        """Raises VenueOutcomeUnknown when the result cannot be known."""

    def query_order(self, account: str, symbol: str, client_id: str) -> OrderQueryResult: ...

    def fetch_account(self, account: str) -> AccountSnapshot: ...

    def poll_fills(self, account: str) -> list[Fill]: ...

    def reference_price(self, symbol: str) -> Decimal | None: ...
