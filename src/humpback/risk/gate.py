"""Pre-trade risk gate.

The gate is the only component that can authorise an order. It returns an ``Approval``: an
HMAC token bound to the intent hash, the client id and an expiry. The signer verifies the token
and refuses anything without one, so a strategy cannot reach a venue by skipping the gate.

Exposure is computed as if every live order fills completely, and ``UNKNOWN_IN_FLIGHT`` orders
are live. This is the "simulate the portfolio after the fill" check: an order is approved only
if the worst-case resulting book stays inside every limit.
"""

from __future__ import annotations

import hashlib
import hmac
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from humpback.core.clock import Clock, utc_day
from humpback.core.errors import SignerRefused
from humpback.core.ids import canonical_json
from humpback.core.money import BPS, ZERO
from humpback.domain.models import OrderIntent, PricingMode, Side, Venue
from humpback.ledger.book import Ledger
from humpback.ledger.orders import OrderView
from humpback.risk.limits import RiskLimits
from humpback.risk.supervisor import DAILY_LOSS, Supervisor, TradingMode
from humpback.venues.base import Action


@dataclass(frozen=True, slots=True)
class Approval:
    intent_id: str
    action: Action
    intent_hash: str
    client_id: str
    expires_at_ms: int
    mac: str

    def message(self) -> bytes:
        return _approval_message(
            self.intent_id, self.action, self.intent_hash, self.client_id, self.expires_at_ms
        )


@dataclass(frozen=True, slots=True)
class Rejection:
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketContext:
    reference_price: Decimal | None
    pricing_mode: PricingMode = PricingMode.NORMAL


PriceLookup = Callable[[Venue, str], Decimal | None]


def _approval_message(
    intent_id: str, action: Action, intent_hash: str, client_id: str, expires_at_ms: int
) -> bytes:
    return canonical_json([intent_id, action.value, intent_hash, client_id, expires_at_ms]).encode()


def mac_for(key: bytes, message: bytes) -> str:
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_approval(key: bytes, approval: Approval, *, now_ms: int) -> None:
    """Used by the signer. Raises ``SignerRefused`` on any problem."""
    expected = mac_for(key, approval.message())
    if not hmac.compare_digest(expected, approval.mac):
        raise SignerRefused("approval MAC does not verify", code="APPROVAL_INVALID")
    if now_ms > approval.expires_at_ms:
        raise SignerRefused("approval expired", code="APPROVAL_EXPIRED")


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

    def evaluate_place(self, view: OrderView, market: MarketContext) -> Approval | Rejection:
        intent = view.intent
        now = self.clock.now_ms()
        reasons: list[str] = []
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
        return self._approve(view, Action.PLACE_ORDER, now)

    def approve_cancel(self, view: OrderView) -> Approval | Rejection:
        """Cancels reduce risk and are allowed in every mode except a manual full halt."""
        if not view.is_live:
            return Rejection(("order_not_live",))
        return self._approve(view, Action.CANCEL_ORDER, self.clock.now_ms())

    # ------------------------------------------------------------------ checks

    def _approve(self, view: OrderView, action: Action, now: int) -> Approval:
        expires = now + self.limits.approval_ttl_ms
        message = _approval_message(
            view.intent.intent_id, action, view.intent.content_hash(), view.client_id, expires
        )
        return Approval(
            view.intent.intent_id,
            action,
            view.intent.content_hash(),
            view.client_id,
            expires,
            mac_for(self._key, message),
        )

    def _price_checks(self, intent: OrderIntent, ref: Decimal) -> list[str]:
        out = []
        for name, price in (("limit", intent.limit_price), ("trigger", intent.trigger_price)):
            if price is None:
                continue
            deviation = abs(price - ref) / ref * BPS
            if deviation > self.limits.max_price_deviation_bps:
                out.append(f"{name}_price_deviation_{deviation.quantize(Decimal(1))}bps")
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
        """Largest absolute position reachable if every live order on the symbol fills."""
        pos = self.ledger.position(venue, account, symbol).quantity
        buys = sells = ZERO
        for order in self.ledger.live_orders(venue):
            o = order.intent
            if o.account != account or o.symbol != symbol:
                continue
            if o.side is Side.BUY:
                buys += order.remaining
            else:
                sells += order.remaining
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
            (o.intent.venue, o.intent.account, o.intent.symbol) for o in self.ledger.live_orders()
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
