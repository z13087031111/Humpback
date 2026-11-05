"""Decimal-only numeric helpers.

Every amount, price, quantity, fee and rate in Humpback is a ``decimal.Decimal``.
Binary floats are rejected at the boundary: ``dec(0.1)`` raises instead of silently
turning into 0.1000000000000000055511151231257827.
"""

from __future__ import annotations

import decimal
from decimal import ROUND_DOWN, ROUND_UP, Decimal, InvalidOperation

# Inputs are capped at MAX_SIGNIFICANT_DIGITS so that sums and products of two inputs fit
# inside the working precision exactly. Every thread gets the same precision.
MAX_SIGNIFICANT_DIGITS = 28
WORKING_PRECISION = 60
decimal.DefaultContext.prec = WORKING_PRECISION
decimal.getcontext().prec = max(decimal.getcontext().prec, WORKING_PRECISION)

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10_000)

DecimalLike = str | int | Decimal


class MoneyError(ValueError):
    """Raised when a value cannot be represented as a finite Decimal."""


def dec(value: DecimalLike) -> Decimal:
    """Convert to a finite Decimal. Floats and bools are rejected on purpose."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise MoneyError(f"decimal input must be str, int or Decimal, got {type(value).__name__}")
    try:
        result = Decimal(value.strip()) if isinstance(value, str) else Decimal(value)
    except InvalidOperation:
        raise MoneyError(f"invalid decimal: {value!r}") from None
    if not result.is_finite():
        raise MoneyError(f"non-finite decimal: {value!r}")
    if len(result.normalize().as_tuple().digits) > MAX_SIGNIFICANT_DIGITS:
        raise MoneyError(f"more than {MAX_SIGNIFICANT_DIGITS} significant digits: {value!r}")
    return result


def positive(value: DecimalLike, name: str = "value") -> Decimal:
    result = dec(value)
    if result <= ZERO:
        raise MoneyError(f"{name} must be > 0, got {result}")
    return result


def non_negative(value: DecimalLike, name: str = "value") -> Decimal:
    result = dec(value)
    if result < ZERO:
        raise MoneyError(f"{name} must be >= 0, got {result}")
    return result


def to_wire(value: DecimalLike) -> str:
    """Canonical text form: no exponent, no trailing zeros, never ``-0``.

    This is the form exchanges expect in signed payloads (Hyperliquid's signing guide
    lists trailing zeros as a common cause of bad signatures).
    """
    exact = dec(value)
    normalized = exact.normalize()
    if normalized != exact:  # pragma: no cover - guarded by the digit cap in dec()
        raise MoneyError(f"canonical form changed the value of {value!r}")
    if normalized == ZERO:
        return "0"
    return format(normalized, "f")


def is_multiple(value: Decimal, step: Decimal, base: Decimal = ZERO) -> bool:
    """True when ``value - base`` is an exact multiple of ``step``."""
    if step <= ZERO:
        raise MoneyError("step must be > 0")
    return (value - base) % step == ZERO


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    """Round toward zero onto the ``step`` grid."""
    if step <= ZERO:
        raise MoneyError("step must be > 0")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_up(value: Decimal, step: Decimal) -> Decimal:
    """Round away from zero onto the ``step`` grid."""
    if step <= ZERO:
        raise MoneyError("step must be > 0")
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def decimal_places(value: Decimal) -> int:
    """Number of digits after the decimal point once trailing zeros are removed."""
    exponent = value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):  # pragma: no cover - finite values only
        raise MoneyError("non-finite decimal")
    return max(0, -exponent)


def significant_figures(value: Decimal) -> int:
    """Count of significant digits, ignoring trailing zeros."""
    normalized = value.normalize()
    if normalized == ZERO:
        return 0
    return len(normalized.as_tuple().digits)
