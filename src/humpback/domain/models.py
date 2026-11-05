"""Immutable domain objects. Decimal everywhere; timestamps are UTC milliseconds."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from humpback.core.ids import content_hash
from humpback.core.money import ZERO, dec, positive, to_wire


class Venue(StrEnum):
    HYPERLIQUID = "hyperliquid"
    LIGHTER = "lighter"
    ASTER = "aster"
    SIM = "sim"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> Decimal:
        return Decimal(1) if self is Side.BUY else Decimal(-1)

    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"
    TAKE_PROFIT_MARKET = "take_profit_market"
    TAKE_PROFIT_LIMIT = "take_profit_limit"


class TimeInForce(StrEnum):
    GTC = "gtc"
    IOC = "ioc"
    POST_ONLY = "post_only"


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    EQUITY = "equity"


class PricingMode(StrEnum):
    """How the venue is currently pricing an instrument.

    Equity perps change behaviour when the underlying market is closed (Aster switches to an
    EWMA-smoothed mark, Lighter falls back to internal pricing when the oracle is stale).
    Risk limits can be tightened per mode.
    """

    NORMAL = "normal"
    OFF_HOURS_SMOOTHED = "off_hours_smoothed"
    INTERNAL_FALLBACK = "internal_fallback"


@dataclass(frozen=True, slots=True)
class Instrument:
    venue: Venue
    symbol: str
    asset_class: AssetClass = AssetClass.CRYPTO
    quote_asset: str = "USDC"

    @property
    def key(self) -> tuple[Venue, str]:
        return (self.venue, self.symbol)


_STOP_TYPES = frozenset(
    {
        OrderType.STOP_MARKET,
        OrderType.STOP_LIMIT,
        OrderType.TAKE_PROFIT_MARKET,
        OrderType.TAKE_PROFIT_LIMIT,
    }
)
_PRICED_TYPES = frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT_LIMIT})


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """One trading decision, before risk approval and signing.

    ``business_key`` identifies the decision. Submitting the same business key twice is a
    no-op that returns the existing order; submitting it with different content is an error.
    """

    intent_id: str
    business_key: str
    account: str
    venue: Venue
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    created_at_ms: int
    trace_id: str
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    reduce_only: bool = False
    strategy: str = "manual"
    tags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        positive(self.quantity, "quantity")
        if not self.business_key or len(self.business_key) > 200:
            raise ValueError("business_key must be 1..200 characters")
        if self.order_type in _PRICED_TYPES:
            if self.limit_price is None:
                raise ValueError(f"{self.order_type} requires limit_price")
            positive(self.limit_price, "limit_price")
        elif self.limit_price is not None:
            raise ValueError(f"{self.order_type} must not carry limit_price")
        if self.order_type in _STOP_TYPES:
            if self.trigger_price is None:
                raise ValueError(f"{self.order_type} requires trigger_price")
            positive(self.trigger_price, "trigger_price")
        elif self.trigger_price is not None:
            raise ValueError(f"{self.order_type} must not carry trigger_price")
        if self.order_type is OrderType.MARKET and self.time_in_force is TimeInForce.POST_ONLY:
            raise ValueError("market orders cannot be post-only")

    def content(self) -> dict[str, Any]:
        """Fields that define *what* is being traded. Used for idempotency checks."""
        return {
            "business_key": self.business_key,
            "account": self.account,
            "venue": self.venue.value,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": to_wire(self.quantity),
            "limit_price": None if self.limit_price is None else to_wire(self.limit_price),
            "trigger_price": None if self.trigger_price is None else to_wire(self.trigger_price),
            "time_in_force": self.time_in_force.value,
            "reduce_only": self.reduce_only,
        }

    def content_hash(self) -> str:
        return content_hash(self.content())

    def to_record(self) -> dict[str, Any]:
        record = self.content()
        record.update(
            intent_id=self.intent_id,
            created_at_ms=self.created_at_ms,
            trace_id=self.trace_id,
            strategy=self.strategy,
            tags=list(self.tags),
        )
        return record

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> OrderIntent:
        return cls(
            intent_id=record["intent_id"],
            business_key=record["business_key"],
            account=record["account"],
            venue=Venue(record["venue"]),
            symbol=record["symbol"],
            side=Side(record["side"]),
            order_type=OrderType(record["order_type"]),
            quantity=Decimal(record["quantity"]),
            created_at_ms=int(record["created_at_ms"]),
            trace_id=record["trace_id"],
            limit_price=None if record["limit_price"] is None else Decimal(record["limit_price"]),
            trigger_price=(
                None if record["trigger_price"] is None else Decimal(record["trigger_price"])
            ),
            time_in_force=TimeInForce(record["time_in_force"]),
            reduce_only=bool(record["reduce_only"]),
            strategy=record.get("strategy", "manual"),
            tags=tuple(record.get("tags", ())),
        )


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution reported by a venue. ``venue_fill_id`` is unique per venue."""

    venue: Venue
    venue_fill_id: str
    intent_id: str
    account: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_asset: str
    is_maker: bool
    filled_at_ms: int

    def __post_init__(self) -> None:
        positive(self.quantity, "fill quantity")
        positive(self.price, "fill price")
        if not self.venue_fill_id:
            raise ValueError("venue_fill_id is required")
        # Fees may be negative (maker rebates), but never NaN/inf.
        dec(self.fee)

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price

    def to_record(self) -> dict[str, Any]:
        return {
            "venue": self.venue.value,
            "venue_fill_id": self.venue_fill_id,
            "intent_id": self.intent_id,
            "account": self.account,
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": to_wire(self.quantity),
            "price": to_wire(self.price),
            "fee": to_wire(self.fee),
            "fee_asset": self.fee_asset,
            "is_maker": self.is_maker,
            "filled_at_ms": self.filled_at_ms,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Fill:
        return cls(
            venue=Venue(record["venue"]),
            venue_fill_id=record["venue_fill_id"],
            intent_id=record["intent_id"],
            account=record["account"],
            symbol=record["symbol"],
            side=Side(record["side"]),
            quantity=Decimal(record["quantity"]),
            price=Decimal(record["price"]),
            fee=Decimal(record["fee"]),
            fee_asset=record["fee_asset"],
            is_maker=bool(record["is_maker"]),
            filled_at_ms=int(record["filled_at_ms"]),
        )


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    """A venue's view of one position, used by reconciliation."""

    venue: Venue
    account: str
    symbol: str
    quantity: Decimal  # signed: > 0 long, < 0 short
    entry_price: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class OpenOrderSnapshot:
    venue: Venue
    account: str
    symbol: str
    client_id: str
    venue_order_id: str
    side: Side
    remaining: Decimal


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Everything a venue says about one account at one moment."""

    venue: Venue
    account: str
    taken_at_ms: int
    collateral: Decimal
    positions: tuple[PositionSnapshot, ...] = ()
    open_orders: tuple[OpenOrderSnapshot, ...] = ()
