"""Per-venue precision rules. Each venue keeps its own grammar; nothing is forced into one model.

* Aster (Binance-style filters): PRICE_FILTER tickSize, LOT_SIZE / MARKET_LOT_SIZE stepSize,
  MIN_NOTIONAL (market orders use the mark price). Checked as ``(x - min) % step == 0``.
* Hyperliquid: size rounded to ``szDecimals``; price at most 5 significant figures and at most
  ``MAX_DECIMALS - szDecimals`` decimals (6 for perps, 8 for spot); integer prices always valid.
* Lighter: integers on the wire, ``size × 10^size_decimals`` and ``price × 10^price_decimals``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from humpback.core.errors import PrecisionError
from humpback.core.money import (
    ZERO,
    decimal_places,
    is_multiple,
    quantize_down,
    quantize_up,
    significant_figures,
)
from humpback.domain.models import OrderIntent, OrderType, Side


def _protective(price: Decimal, step: Decimal, side: Side) -> Decimal:
    """Round onto a grid in the direction that keeps an aggressive close marketable:
    sells round down, buys round up."""
    return quantize_down(price, step) if side is Side.SELL else quantize_up(price, step)


def _order_price(intent: OrderIntent, reference_price: Decimal | None) -> Decimal | None:
    """The price used for notional checks: the limit price, else the reference (mark) price."""
    return intent.limit_price if intent.limit_price is not None else reference_price


@dataclass(frozen=True, slots=True)
class TickStepRules:
    """Aster V3 symbol filters, taken from ``/fapi/v3/exchangeInfo``."""

    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal

    def _price(self, price: Decimal, name: str) -> None:
        if self.min_price > ZERO and price < self.min_price:
            raise PrecisionError(f"{name} {price} below minPrice {self.min_price}")
        if self.max_price > ZERO and price > self.max_price:
            raise PrecisionError(f"{name} {price} above maxPrice {self.max_price}")
        if self.tick_size > ZERO and not is_multiple(price, self.tick_size, self.min_price):
            raise PrecisionError(f"{name} {price} not on tickSize {self.tick_size}")

    def round_price(self, price: Decimal, side: Side) -> Decimal:
        if self.tick_size <= ZERO:
            return price
        return self.min_price + _protective(price - self.min_price, self.tick_size, side)

    def check(self, intent: OrderIntent, reference_price: Decimal | None) -> None:
        qty = intent.quantity
        if intent.order_type is OrderType.MARKET:
            step, lo, hi = self.market_step_size, self.market_min_qty, self.market_max_qty
        else:
            step, lo, hi = self.step_size, self.min_qty, self.max_qty
        if qty < lo or qty > hi:
            raise PrecisionError(f"quantity {qty} outside [{lo}, {hi}]")
        if not is_multiple(qty, step, lo):
            raise PrecisionError(f"quantity {qty} not on stepSize {step}")
        if intent.limit_price is not None:
            self._price(intent.limit_price, "price")
        if intent.trigger_price is not None:
            self._price(intent.trigger_price, "stopPrice")
        price = _order_price(intent, reference_price)
        if price is None:
            raise PrecisionError("min-notional check needs a limit or reference price")
        if not intent.reduce_only and qty * price < self.min_notional:
            raise PrecisionError(f"notional {qty * price} below minNotional {self.min_notional}")


@dataclass(frozen=True, slots=True)
class HyperliquidRules:
    sz_decimals: int
    is_spot: bool = False
    # Minimum order value. The legacy code used 10 USDC; re-verify against current docs.
    min_notional: Decimal = Decimal(10)

    @property
    def max_decimals(self) -> int:
        return 8 if self.is_spot else 6

    def round_price(self, price: Decimal, side: Side) -> Decimal:
        """Largest grid the rules allow: 5 significant figures and at most
        ``max_decimals - sz_decimals`` decimals; integers are always allowed."""
        allowed = self.max_decimals - self.sz_decimals
        by_sig_figs = 4 - price.adjusted()
        places = max(min(allowed, by_sig_figs), 0)
        rounded = _protective(price, Decimal(1).scaleb(-places), side)
        self.check_price(rounded)
        return rounded

    def check_price(self, price: Decimal, name: str = "price") -> None:
        if price <= ZERO:
            raise PrecisionError(f"{name} must be positive")
        if price == price.to_integral_value():
            return  # integer prices are always allowed
        if significant_figures(price) > 5:
            raise PrecisionError(f"{name} {price} has more than 5 significant figures")
        allowed = self.max_decimals - self.sz_decimals
        if decimal_places(price) > allowed:
            raise PrecisionError(f"{name} {price} has more than {allowed} decimals")

    def check(self, intent: OrderIntent, reference_price: Decimal | None) -> None:
        if decimal_places(intent.quantity) > self.sz_decimals:
            raise PrecisionError(f"size {intent.quantity} exceeds szDecimals {self.sz_decimals}")
        if intent.limit_price is not None:
            self.check_price(intent.limit_price)
        if intent.trigger_price is not None:
            self.check_price(intent.trigger_price, "triggerPx")
        price = _order_price(intent, reference_price)
        if price is None:
            raise PrecisionError("min-notional check needs a limit or reference price")
        if not intent.reduce_only and intent.quantity * price < self.min_notional:
            raise PrecisionError(f"order value {intent.quantity * price} below {self.min_notional}")


@dataclass(frozen=True, slots=True)
class LighterRules:
    """From ``orderBookDetails``: integer scaling plus minimum base and quote amounts."""

    size_decimals: int
    price_decimals: int
    min_base_amount: Decimal
    min_quote_amount: Decimal

    def round_price(self, price: Decimal, side: Side) -> Decimal:
        return _protective(price, Decimal(1).scaleb(-self.price_decimals), side)

    def to_base_int(self, quantity: Decimal) -> int:
        if decimal_places(quantity) > self.size_decimals:
            raise PrecisionError(f"size {quantity} exceeds size_decimals {self.size_decimals}")
        return int(quantity.scaleb(self.size_decimals))

    def to_price_int(self, price: Decimal) -> int:
        if decimal_places(price) > self.price_decimals:
            raise PrecisionError(f"price {price} exceeds price_decimals {self.price_decimals}")
        return int(price.scaleb(self.price_decimals))

    def check(self, intent: OrderIntent, reference_price: Decimal | None) -> None:
        self.to_base_int(intent.quantity)
        if intent.limit_price is not None:
            self.to_price_int(intent.limit_price)
        if intent.trigger_price is not None:
            self.to_price_int(intent.trigger_price)
        if intent.quantity < self.min_base_amount:
            raise PrecisionError(f"size {intent.quantity} below min {self.min_base_amount}")
        price = _order_price(intent, reference_price)
        if price is None:
            raise PrecisionError("min-quote check needs a limit or reference price")
        if not intent.reduce_only and intent.quantity * price < self.min_quote_amount:
            raise PrecisionError(f"quote {intent.quantity * price} below {self.min_quote_amount}")
