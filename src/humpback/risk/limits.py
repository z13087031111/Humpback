"""Risk limits. Plain data, loaded from TOML; every value is a Decimal or an int."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from humpback.core.money import dec, positive
from humpback.domain.models import PricingMode


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal
    max_symbol_notional: Decimal
    max_gross_notional: Decimal
    max_daily_loss: Decimal
    max_orders_per_minute: int
    max_price_deviation_bps: Decimal
    allowed_symbols: frozenset[tuple[str, str]]  # (venue, symbol)
    approval_ttl_ms: int = 5_000
    # Multiplier on notional limits when an equity perp is not normally priced.
    pricing_mode_scale: dict[PricingMode, Decimal] = field(
        default_factory=lambda: {
            PricingMode.NORMAL: Decimal(1),
            PricingMode.OFF_HOURS_SMOOTHED: Decimal("0.5"),
            PricingMode.INTERNAL_FALLBACK: Decimal("0.25"),
        }
    )

    def __post_init__(self) -> None:
        for name in (
            "max_order_notional",
            "max_symbol_notional",
            "max_gross_notional",
            "max_daily_loss",
            "max_price_deviation_bps",
        ):
            positive(getattr(self, name), name)
        if self.max_orders_per_minute <= 0 or self.approval_ttl_ms <= 0:
            raise ValueError("rate and ttl limits must be positive")
        if self.max_order_notional > self.max_symbol_notional:
            raise ValueError("max_order_notional cannot exceed max_symbol_notional")
        if self.max_symbol_notional > self.max_gross_notional:
            raise ValueError("max_symbol_notional cannot exceed max_gross_notional")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> RiskLimits:
        scale = data.get("pricing_mode_scale")
        kwargs: dict[str, Any] = {
            "max_order_notional": dec(str(data["max_order_notional"])),
            "max_symbol_notional": dec(str(data["max_symbol_notional"])),
            "max_gross_notional": dec(str(data["max_gross_notional"])),
            "max_daily_loss": dec(str(data["max_daily_loss"])),
            "max_orders_per_minute": int(data["max_orders_per_minute"]),
            "max_price_deviation_bps": dec(str(data["max_price_deviation_bps"])),
            "allowed_symbols": frozenset(
                (str(v), str(s)) for v, s in (item.split(":", 1) for item in data["allowed"])
            ),
            "approval_ttl_ms": int(data.get("approval_ttl_ms", 5_000)),
        }
        if scale:
            kwargs["pricing_mode_scale"] = {PricingMode(k): dec(str(v)) for k, v in scale.items()}
        return cls(**kwargs)

    @classmethod
    def load(cls, path: str | Path) -> RiskLimits:
        """TOML numbers must be written as strings (``"1000"``) to avoid binary floats."""
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
        for key, value in data.get("risk", {}).items():
            if isinstance(value, float):
                raise ValueError(f"risk.{key} is a float; write it as a string")
        return cls.from_mapping(data["risk"])
