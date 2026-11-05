from decimal import Decimal

import pytest

from humpback.core.money import (
    MoneyError,
    dec,
    decimal_places,
    is_multiple,
    quantize_down,
    quantize_up,
    significant_figures,
    to_wire,
)


@pytest.mark.parametrize("bad", [0.1, True, "nan", "inf", "abc", None])
def test_dec_rejects_floats_bools_and_non_finite(bad: object) -> None:
    with pytest.raises(MoneyError):
        dec(bad)  # type: ignore[arg-type]


def test_dec_accepts_exact_inputs() -> None:
    assert dec("0.1") + dec("0.2") == Decimal("0.3")
    assert dec(5) == Decimal(5)


@pytest.mark.parametrize(
    ("value", "wire"),
    [
        ("100.000", "100"),
        ("1670.10", "1670.1"),
        ("0.0147", "0.0147"),
        ("-0", "0"),
        ("1E+2", "100"),
        ("0.00000001", "0.00000001"),
    ],
)
def test_to_wire_has_no_trailing_zeros_or_exponent(value: str, wire: str) -> None:
    assert to_wire(value) == wire


def test_grid_helpers() -> None:
    assert is_multiple(Decimal("1.25"), Decimal("0.05"))
    assert not is_multiple(Decimal("1.26"), Decimal("0.05"))
    assert is_multiple(Decimal("0.0012"), Decimal("0.001"), base=Decimal("0.0002"))
    assert quantize_down(Decimal("1.239"), Decimal("0.01")) == Decimal("1.23")
    assert quantize_up(Decimal("1.231"), Decimal("0.01")) == Decimal("1.24")
    assert significant_figures(Decimal("1234.50")) == 5
    assert decimal_places(Decimal("0.0012300")) == 5
