"""Pre-trade risk gate.

The gate is the only component that can authorise an order. It returns an ``Approval``: an
HMAC token bound to the action, venue, account, intent hash, client id, a hash of the exact
request body, and an expiry. The signer verifies the token and recomputes the body hash, so a
strategy cannot reach a venue by skipping the gate, and an approval for one request (say, a
cancel) cannot be reused to sign a different one (say, a new order).

Exposure is computed as if every live order fills completely, and ``UNKNOWN_IN_FLIGHT`` orders
are live. This is the "simulate the portfolio after the fill" check: an order is approved only
if the worst-case resulting book stays inside every limit.
"""

from __future__ import annotations

import hashlib
import hmac
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal

from humpback.core.clock import Clock, utc_day
from humpback.core.errors import SignerRefused
from humpback.core.ids import canonical_json, content_hash
from humpback.core.money import BPS, ZERO
from humpback.domain.models import OrderIntent, PricingMode, Side, Venue
from humpback.ledger.book import Ledger
from humpback.ledger.orders import OrderStatus, OrderView
from humpback.risk.limits import RiskLimits
from humpback.risk.supervisor import DAILY_LOSS, Supervisor, TradingMode
from humpback.venues.base import Action, UnsignedRequest


@dataclass(frozen=True, slots=True)
class Approval:
    intent_id: str
    action: Action
    venue: Venue
    account: str
    intent_hash: str
    client_id: str
    body_hash: str
    expires_at_ms: int
    mac: str

    def message(self) -> bytes:
        return canonical_json(
            [
                self.intent_id,
                self.action.value,
                self.venue.value,
                self.account,
                self.intent_hash,
                self.client_id,
                self.body_hash,
                self.expires_at_ms,
            ]
        ).encode()


def request_body_hash(request: UnsignedRequest) -> str:
    return content_hash(request.body)


@dataclass(frozen=True, slots=True)
class Rejection:
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketContext:
    reference_price: Decimal | None
    pricing_mode: PricingMode = PricingMode.NORMAL


PriceLookup = Callable[[Venue, str], Decimal | None]


def mac_for(key: bytes, message: bytes) -> str:
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_approval(
    key: bytes, approval: Approval, request: UnsignedRequest, *, now_ms: int
) -> None:
    """Used by the signer. Raises ``SignerRefused`` on any problem."""
    expected = mac_for(key, approval.message())
    if not hmac.compare_digest(expected, approval.mac):
        raise SignerRefused("approval MAC does not verify", code="APPROVAL_INVALID")
    if now_ms > approval.expires_at_ms:
        raise SignerRefused("approval expired", code="APPROVAL_EXPIRED")
    mismatches = [
        name
        for name, ours, theirs in (
            ("intent_id", request.intent_id, approval.intent_id),
            ("action", request.action, approval.action),
            ("venue", request.venue, approval.venue),
            ("account", request.account, approval.account),
            ("intent_hash", request.intent_hash, approval.intent_hash),
            ("client_id", request.client_id, approval.client_id),
            ("body_hash", request_body_hash(request), approval.body_hash),
        )
        if ours != theirs
    ]
    if mismatches:
        raise SignerRefused(
            f"approval does not match request: {mismatches}", code="APPROVAL_MISMATCH"
        )


class RiskGate:
    def __init__(
        self,
        *,
        limits: RiskLimits,
        supervisor: Supervisor,
        ledger: Ledger,
        clock: Clock,
        approval_key: bytes,
        price_lookup: PriceLookup,
    ) -> None:
        if len(approval_key) < 32:
            raise ValueError("approval key must be at least 32 bytes")
        self.limits = limits
        self.supervisor = supervisor
        self.ledger = ledger
        self.clock = clock
        self._key = approval_key
        self._price_lookup = price_lookup
        self._recent: deque[int] = deque()

    # ------------------------------------------------------------------ public

    def evaluate_place(
        self, view: OrderView, market: MarketContext, request: UnsignedRequest
    ) -> Approval | Rejection:
        try:
            return self._evaluate_place(view, market, request)
        except Exception as exc:  # a broken check must reject, never leave the order hanging
            return Rejection((f"gate_error:{type(exc).__name__}",))

    def _evaluate_place(
        self, view: OrderView, market: MarketContext, request: UnsignedRequest
    ) -> Approval | Rejection:
        intent = view.intent
        now = self.clock.now_ms()
        reasons: list[str] = []
        if view.status is not OrderStatus.CREATED:
            return Rejection(("order_not_new",))
        mismatch = self._request_mismatch(view, request, Action.PLACE_ORDER)
        if mismatch:
            return Rejection((mismatch,))
        reducing = intent.reduce_only and self._reduces(view)
        if intent.reduce_only and not reducing:
            reasons.append("reduce_only_exceeds_position")

        mode = self.supervisor.mode
        if mode is TradingMode.HALTED:
            reasons.append("mode_halted")
        elif mode is not TradingMode.NORMAL and not reducing:
            reasons.append(f"mode_{mode.name.lower()}_blocks_new_risk")

        if (intent.venue.value, intent.symbol) not in self.limits.allowed_symbols:
            reasons.append("symbol_not_allowed")

        ref = market.reference_price
        if ref is None or ref <= ZERO:
            reasons.append("no_reference_price")
        else:
            reasons.extend(self._price_checks(intent, ref))
            if not reducing:
                reasons.extend(self._exposure_checks(view, ref, market.pricing_mode))

        if not reducing and self._daily_loss_hit(intent, now):
            reasons.append("daily_loss_limit")

        self._trim_rate_window(now)
        if len(self._recent) >= self.limits.max_orders_per_minute:
            reasons.append("order_rate_limit")

        if reasons:
            return Rejection(tuple(reasons))
        self._recent.append(now)
        return self._approve(view, request, now)

    def approve_cancel(self, view: OrderView, request: UnsignedRequest) -> Approval | Rejection:
        """Cancels reduce risk and are allowed in every mode."""
        if not view.is_live:
            return Rejection(("order_not_live",))
        mismatch = self._request_mismatch(view, request, Action.CANCEL_ORDER)
        if mismatch:
            return Rejection((mismatch,))
        return self._approve(view, request, self.clock.now_ms())

    # ------------------------------------------------------------------ checks

    @staticmethod
    def _request_mismatch(view: OrderView, request: UnsignedRequest, action: Action) -> str | None:
        intent = view.intent
        if (
            request.action is not action
            or request.venue is not intent.venue
            or request.account != intent.account
            or request.intent_id != intent.intent_id
            or request.client_id != view.client_id
            or request.intent_hash != intent.content_hash()
        ):
            return "request_does_not_match_order"
        return None

    def _approve(self, view: OrderView, request: UnsignedRequest, now: int) -> Approval:
        unsigned = Approval(
            intent_id=view.intent.intent_id,
            action=request.action,
            venue=request.venue,
            account=request.account,
            intent_hash=view.intent.content_hash(),
            client_id=view.client_id,
            body_hash=request_body_hash(request),
            expires_at_ms=now + self.limits.approval_ttl_ms,
            mac="",
        )
        return replace(unsigned, mac=mac_for(self._key, unsigned.message()))

    def _price_checks(self, intent: OrderIntent, ref: Decimal) -> list[str]:
        out = []
        for name, price in (("limit", intent.limit_price), ("trigger", intent.trigger_price)):
            if price is None:
                continue
            deviation = abs(price - ref) / ref * BPS
            if deviation > self.limits.max_price_deviation_bps:
                out.append(f"{name}_price_deviation_{deviation.to_integral_value()}bps")
        return out

    def _scale(self, mode: PricingMode) -> Decimal:
        return self.limits.pricing_mode_scale.get(mode, Decimal(1))

    def _exposure_checks(self, view: OrderView, ref: Decimal, mode: PricingMode) -> list[str]:
        intent = view.intent
        scale = self._scale(mode)
        out = []
        price = intent.limit_price or ref
        if intent.quantity * price > self.limits.max_order_notional * scale:
            out.append("order_notional_limit")
        worst = self._worst_case_qty(
            intent.venue, intent.account, intent.symbol, extra=(intent.side, intent.quantity)
        )
        if worst * ref > self.limits.max_symbol_notional * scale:
            out.append("symbol_exposure_limit")
        gross = self._gross_exposure(intent, ref)
        if gross is None:
            out.append("no_price_for_gross_exposure")
        elif gross > self.limits.max_gross_notional:
            out.append("gross_exposure_limit")
        return out

    def _worst_case_qty(
        self, venue: Venue, account: str, symbol: str, extra: tuple[Side, Decimal] | None = None
    ) -> Decimal:
        """Largest absolute position reachable if every live order on the symbol fills.

        Fills the venue has reported but that are not booked yet count as well.
        """
        pos = self.ledger.position(venue, account, symbol).quantity
        buys = sells = ZERO
        for order in self.ledger.exposure_orders(venue):
            o = order.intent
            if o.account != account or o.symbol != symbol:
                continue
            qty = order.remaining if order.is_live else order.unbooked_qty
            if o.side is Side.BUY:
                buys += qty
            else:
                sells += qty
        if extra is not None:
            side, qty = extra
            if side is Side.BUY:
                buys += qty
            else:
                sells += qty
        return max(abs(pos + buys), abs(pos - sells))

    def _gross_exposure(self, intent: OrderIntent, ref: Decimal) -> Decimal | None:
        symbols: set[tuple[Venue, str, str]] = {
            (v, a, s) for (v, a, s), p in self.ledger.positions().items() if p.quantity != ZERO
        }
        symbols |= {
            (o.intent.venue, o.intent.account, o.intent.symbol)
            for o in self.ledger.exposure_orders()
        }
        symbols.add((intent.venue, intent.account, intent.symbol))
        total = ZERO
        for venue, account, symbol in symbols:
            this = venue is intent.venue and account == intent.account and symbol == intent.symbol
            extra = (intent.side, intent.quantity) if this else None
            qty = self._worst_case_qty(venue, account, symbol, extra)
            price = ref if this else self._price_lookup(venue, symbol)
            if price is None:
                return None
            total += qty * price
        return total

    def _reduces(self, view: OrderView) -> bool:
        intent = view.intent
        pos = self.ledger.position(intent.venue, intent.account, intent.symbol).quantity
        if pos == ZERO or (pos > ZERO) == (intent.side is Side.BUY):
            return False
        pending = sum(
            (
                o.remaining
                for o in self.ledger.live_orders(intent.venue)
                if o.intent.intent_id != intent.intent_id
                and o.intent.account == intent.account
                and o.intent.symbol == intent.symbol
                and o.intent.reduce_only
                and o.intent.side is intent.side
            ),
            ZERO,
        )
        return intent.quantity + pending <= abs(pos)

    def _daily_loss_hit(self, intent: OrderIntent, now: int) -> bool:
        realized = self.ledger.realized_on_day(intent.venue, intent.account, utc_day(now))
        if realized <= -self.limits.max_daily_loss:
            self.supervisor.raise_reason(
                DAILY_LOSS, TradingMode.REDUCE_ONLY, f"realized {realized} on {intent.venue}", now
            )
            return True
        return False

    def _trim_rate_window(self, now: int) -> None:
        while self._recent and now - self._recent[0] >= 60_000:
            self._recent.popleft()
