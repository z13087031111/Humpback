"""Simulated venue for L1 (local) verification, with fault injection.

Matching follows the conservative rules kept from the legacy Decimal model
(``chainpulse_v10/execution/model.py``): marketable orders consume visible depth once;
passive orders rest and are never filled by assumption, only when the test trades through
their price; post-only orders that would cross are rejected.

Faults model what real venues do to us:

* ``DROP_REQUEST``   – the request never arrives; the caller sees an unknown outcome;
* ``LOSE_RESPONSE``  – the venue processes the request but the reply is lost;
* ``REJECT``         – a definitive rejection with a reason;
* ``DUPLICATE_FILLS``– every fill is reported twice;
* ``DELAY_FILLS``    – fills are held back until ``release_delayed_fills``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from humpback.core.clock import Clock
from humpback.core.errors import UnsupportedCapability, VenueOutcomeUnknown
from humpback.core.money import BPS, ZERO
from humpback.domain.models import (
    AccountSnapshot,
    Fill,
    OpenOrderSnapshot,
    OrderIntent,
    OrderType,
    PositionSnapshot,
    Side,
    TimeInForce,
    Venue,
)
from humpback.ledger.book import Position
from humpback.venues.base import (
    Action,
    OrderQueryResult,
    SignedRequest,
    SubmitOutcome,
    SubmitResult,
    UnsignedRequest,
    VenueCapabilities,
)
from humpback.venues.precision import TickStepRules

CAPABILITIES = VenueCapabilities(
    venue=Venue.SIM,
    order_types=frozenset({OrderType.LIMIT, OrderType.MARKET}),
    time_in_force=frozenset({TimeInForce.GTC, TimeInForce.IOC, TimeInForce.POST_ONLY}),
    reduce_only=True,
    query_by_client_id=True,
    cancel_by_client_id=True,
    client_id_format="0x + 32 hex",
    signature_scheme="test backend",
    nonce_unit="ms",
)


class Fault(StrEnum):
    DROP_REQUEST = "drop_request"
    LOSE_RESPONSE = "lose_response"
    REJECT = "reject"
    DUPLICATE_FILLS = "duplicate_fills"
    DELAY_FILLS = "delay_fills"


@dataclass
class _Order:
    intent: OrderIntent
    client_id: str
    venue_order_id: str
    status: str = "open"
    filled: Decimal = ZERO

    @property
    def remaining(self) -> Decimal:
        return self.intent.quantity - self.filled


@dataclass
class _Account:
    collateral: Decimal = ZERO
    positions: dict[str, Position] = field(default_factory=dict)


class SimVenue:
    venue = Venue.SIM

    def __init__(
        self,
        clock: Clock,
        *,
        taker_fee_bps: Decimal = Decimal("4.5"),
        maker_fee_bps: Decimal = Decimal("1.5"),
        rules: TickStepRules | None = None,
    ) -> None:
        self.clock = clock
        self.taker_fee_bps = taker_fee_bps
        self.maker_fee_bps = maker_fee_bps
        self._rules = rules or TickStepRules(
            tick_size=Decimal("0.01"),
            min_price=Decimal("0.01"),
            max_price=Decimal(10_000_000),
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal(1_000_000),
            market_step_size=Decimal("0.001"),
            market_min_qty=Decimal("0.001"),
            market_max_qty=Decimal(1_000_000),
            min_notional=Decimal(5),
        )
        self.authorized_signers: set[str] = set()
        self._books: dict[
            str, tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]
        ] = {}
        self._orders: dict[str, _Order] = {}
        self._accounts: dict[str, _Account] = {}
        self._faults: deque[tuple[Fault, str]] = deque()
        self._sticky: set[Fault] = set()
        self._pending: dict[str, list[Fill]] = {}
        self._delayed: dict[str, list[Fill]] = {}
        self._seq = 0
        self._fault_for_fills: Fault | None = None
        self.requests_received = 0

    # ------------------------------------------------------------------ codec

    @property
    def capabilities(self) -> VenueCapabilities:
        return CAPABILITIES

    def rules(self, symbol: str) -> TickStepRules:
        return self._rules

    def encode_client_id(self, key: bytes) -> str:
        return "0x" + key.hex()

    def build_place(self, intent: OrderIntent, client_id: str) -> UnsignedRequest:
        CAPABILITIES.ensure_supported(intent)
        return UnsignedRequest(
            Venue.SIM,
            Action.PLACE_ORDER,
            intent.account,
            intent.intent_id,
            client_id,
            intent.content_hash(),
            {"op": "place", "intent": intent.to_record()},
        )

    def build_cancel(self, intent: OrderIntent, client_id: str) -> UnsignedRequest:
        return UnsignedRequest(
            Venue.SIM,
            Action.CANCEL_ORDER,
            intent.account,
            intent.intent_id,
            client_id,
            intent.content_hash(),
            {"op": "cancel", "symbol": intent.symbol},
        )

    # ------------------------------------------------------------------ test controls

    def set_book(
        self, symbol: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]
    ) -> None:
        b = sorted(((Decimal(p), Decimal(q)) for p, q in bids), reverse=True)
        a = sorted((Decimal(p), Decimal(q)) for p, q in asks)
        if b and a and b[0][0] >= a[0][0]:
            raise ValueError("crossed book")
        self._books[symbol] = (b, a)

    def deposit(self, account: str, amount: Decimal) -> None:
        self._accounts.setdefault(account, _Account()).collateral += amount

    def inject(self, fault: Fault, reason: str = "", *, sticky: bool = False) -> None:
        if sticky:
            self._sticky.add(fault)
        else:
            self._faults.append((fault, reason))

    def clear_faults(self) -> None:
        self._faults.clear()
        self._sticky.clear()

    def release_delayed_fills(self) -> None:
        for account, fills in self._delayed.items():
            self._pending.setdefault(account, []).extend(fills)
        self._delayed.clear()

    def trade_through(self, symbol: str, price: Decimal) -> None:
        """Simulate the market trading through ``price``: resting orders at or better fill."""
        for order in list(self._orders.values()):
            intent = order.intent
            if order.status != "open" or intent.symbol != symbol or intent.limit_price is None:
                continue
            crosses = (
                intent.limit_price >= price
                if intent.side is Side.BUY
                else intent.limit_price <= price
            )
            if crosses:
                self._fill(order, order.remaining, intent.limit_price, maker=True)

    # ------------------------------------------------------------------ transport

    def send(self, signed: SignedRequest) -> SubmitResult:
        self.requests_received += 1
        if signed.signer_address not in self.authorized_signers or not signed.signature:
            return SubmitResult(SubmitOutcome.REJECTED, reason="unauthorized signer")
        fault, reason = self._next_fault()
        if fault is Fault.DROP_REQUEST:
            raise VenueOutcomeUnknown("simulated timeout before the venue received the request")
        if fault is Fault.REJECT:
            return SubmitResult(SubmitOutcome.REJECTED, reason=reason or "simulated reject")
        body = signed.request.body
        if body["op"] == "place":
            result = self._place(
                OrderIntent.from_record(body["intent"]), signed.request.client_id, fault
            )
        elif body["op"] == "cancel":
            result = self._cancel(signed.request.client_id)
        else:  # pragma: no cover - codec only builds the two ops above
            raise UnsupportedCapability(f"sim op {body['op']}")
        if fault is Fault.LOSE_RESPONSE:
            raise VenueOutcomeUnknown("simulated timeout after the venue processed the request")
        return result

    def query_order(self, account: str, symbol: str, client_id: str) -> OrderQueryResult:
        order = self._orders.get(client_id)
        if order is None or order.intent.account != account:
            return OrderQueryResult("not_found")
        status: Any = order.status
        return OrderQueryResult(status, order.venue_order_id, order.filled)

    def fetch_account(self, account: str) -> AccountSnapshot:
        acct = self._accounts.get(account, _Account())
        realized = sum((p.realized_pnl - p.fees for p in acct.positions.values()), ZERO)
        return AccountSnapshot(
            venue=Venue.SIM,
            account=account,
            taken_at_ms=self.clock.now_ms(),
            collateral=acct.collateral + realized,
            positions=tuple(
                PositionSnapshot(Venue.SIM, account, s, p.quantity, p.avg_entry)
                for s, p in sorted(acct.positions.items())
                if p.quantity != ZERO
            ),
            open_orders=tuple(
                OpenOrderSnapshot(
                    Venue.SIM,
                    account,
                    o.intent.symbol,
                    o.client_id,
                    o.venue_order_id,
                    o.intent.side,
                    o.remaining,
                )
                for o in self._orders.values()
                if o.status == "open" and o.intent.account == account
            ),
        )

    def poll_fills(self, account: str) -> list[Fill]:
        return self._pending.pop(account, [])

    def reference_price(self, symbol: str) -> Decimal | None:
        book = self._books.get(symbol)
        if not book or not book[0] or not book[1]:
            return None
        return (book[0][0][0] + book[1][0][0]) / 2

    # ------------------------------------------------------------------ internals

    def _next_fault(self) -> tuple[Fault | None, str]:
        for sticky in (Fault.DROP_REQUEST, Fault.LOSE_RESPONSE):
            if sticky in self._sticky:
                return sticky, ""
        if self._faults:
            return self._faults.popleft()
        if Fault.DUPLICATE_FILLS in self._sticky:
            return Fault.DUPLICATE_FILLS, ""
        if Fault.DELAY_FILLS in self._sticky:
            return Fault.DELAY_FILLS, ""
        return None, ""

    def _place(self, intent: OrderIntent, client_id: str, fault: Fault | None) -> SubmitResult:
        if client_id in self._orders:
            return SubmitResult(SubmitOutcome.REJECTED, reason="duplicate client id")
        acct = self._accounts.setdefault(intent.account, _Account())
        if intent.reduce_only:
            pos = acct.positions.get(intent.symbol, Position()).quantity
            reduces = (pos > ZERO and intent.side is Side.SELL) or (
                pos < ZERO and intent.side is Side.BUY
            )
            if not reduces or intent.quantity > abs(pos):
                return SubmitResult(SubmitOutcome.REJECTED, reason="reduce-only would increase")
        bids, asks = self._books.get(intent.symbol, ([], []))
        levels = asks if intent.side is Side.BUY else bids
        limit = intent.limit_price
        touch = levels[0][0] if levels else None
        marketable = touch is not None and (
            limit is None or (limit >= touch if intent.side is Side.BUY else limit <= touch)
        )
        if intent.time_in_force is TimeInForce.POST_ONLY and marketable:
            return SubmitResult(SubmitOutcome.REJECTED, reason="post-only would cross")
        if intent.order_type is OrderType.MARKET and touch is None:
            return SubmitResult(SubmitOutcome.REJECTED, reason="no liquidity")
        self._seq += 1
        order = _Order(intent, client_id, venue_order_id=str(self._seq))
        self._orders[client_id] = order
        self._fault_for_fills = fault
        if marketable:
            for price, size in list(levels):
                if limit is not None and (
                    price > limit if intent.side is Side.BUY else price < limit
                ):
                    break
                take = min(size, order.remaining)
                if take <= ZERO:
                    break
                self._fill(order, take, price, maker=False)
                self._consume(levels, price, take)
                if order.remaining == ZERO:
                    break
        terminal: Literal["filled", "expired", "canceled"] | None = None
        if order.remaining > ZERO and (
            intent.time_in_force is TimeInForce.IOC or intent.order_type is OrderType.MARKET
        ):
            order.status = "expired"
            terminal = "expired"
        elif order.remaining == ZERO:
            terminal = "filled"
        self._fault_for_fills = None
        return SubmitResult(
            SubmitOutcome.ACCEPTED, venue_order_id=order.venue_order_id, terminal_status=terminal
        )

    def _cancel(self, client_id: str) -> SubmitResult:
        order = self._orders.get(client_id)
        if order is None or order.status != "open":
            return SubmitResult(SubmitOutcome.REJECTED, reason="order not open")
        order.status = "canceled"
        return SubmitResult(
            SubmitOutcome.ACCEPTED, venue_order_id=order.venue_order_id, terminal_status="canceled"
        )

    @staticmethod
    def _consume(levels: list[tuple[Decimal, Decimal]], price: Decimal, qty: Decimal) -> None:
        for i, (p, size) in enumerate(levels):
            if p == price:
                left = size - qty
                if left > ZERO:
                    levels[i] = (p, left)
                else:
                    levels.pop(i)
                return

    def _fill(self, order: _Order, qty: Decimal, price: Decimal, *, maker: bool) -> None:
        order.filled += qty
        if order.remaining == ZERO:
            order.status = "filled"
        fee_bps = self.maker_fee_bps if maker else self.taker_fee_bps
        self._seq += 1
        intent = order.intent
        fill = Fill(
            venue=Venue.SIM,
            venue_fill_id=f"f{self._seq}",
            intent_id=intent.intent_id,
            account=intent.account,
            symbol=intent.symbol,
            side=intent.side,
            quantity=qty,
            price=price,
            fee=qty * price * fee_bps / BPS,
            fee_asset="USDC",
            is_maker=maker,
            filled_at_ms=self.clock.now_ms(),
        )
        acct = self._accounts.setdefault(intent.account, _Account())
        pos, _ = acct.positions.get(intent.symbol, Position()).apply_fill(fill)
        acct.positions[intent.symbol] = pos
        fault = self._fault_for_fills or (
            Fault.DUPLICATE_FILLS if Fault.DUPLICATE_FILLS in self._sticky else None
        )
        if fault is Fault.DELAY_FILLS or Fault.DELAY_FILLS in self._sticky:
            self._delayed.setdefault(intent.account, []).append(fill)
            return
        reports = [fill, replace(fill)] if fault is Fault.DUPLICATE_FILLS else [fill]
        self._pending.setdefault(intent.account, []).extend(reports)
