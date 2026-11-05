from decimal import Decimal
from pathlib import Path

import pytest

from humpback.domain.models import PricingMode
from humpback.risk.limits import RiskLimits

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "risk.example.toml"


def test_example_config_loads_as_decimals() -> None:
    limits = RiskLimits.load(EXAMPLE)
    assert limits.max_order_notional == Decimal(500)
    assert ("hyperliquid", "BTC") in limits.allowed_symbols
    assert limits.pricing_mode_scale[PricingMode.OFF_HOURS_SMOOTHED] == Decimal("0.5")


def test_float_values_in_config_are_refused(tmp_path: Path) -> None:
    bad = tmp_path / "risk.toml"
    bad.write_text(EXAMPLE.read_text().replace('max_daily_loss = "150"', "max_daily_loss = 150.5"))
    with pytest.raises(ValueError, match="float"):
        RiskLimits.load(bad)
