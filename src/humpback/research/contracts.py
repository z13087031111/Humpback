"""Phase-3 contracts, fixed now so phase 1 cannot be bent around a strategy later.

* Strategies see only ``MarketView`` (read-only, point-in-time) and return ``TradeProposal``s.
  They never receive a signer, a transport or a ledger handle: every proposal goes through
  the risk gate like any other intent.
* Every feature value carries the time it became *available*; a backtest at time ``t`` may
  only read values with ``available_at_ms <= t``.
* Every backtest run is a ``TrialRecord``, including failures, so the deflated Sharpe ratio
  can use the true number of trials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from humpback.domain.models import Side, Venue


@dataclass(frozen=True, slots=True)
class FeatureValue:
    name: str
    venue: Venue
    symbol: str
    value: Decimal
    event_at_ms: int  # when it happened
    available_at_ms: int  # when Humpback could first have known it
    ingested_at_ms: int  # when it was stored
    source: str


class MarketView(Protocol):
    """Point-in-time, read-only view. Implementations must enforce ``available_at <= now``."""

    @property
    def now_ms(self) -> int: ...

    def feature(self, name: str, venue: Venue, symbol: str) -> FeatureValue | None: ...


@dataclass(frozen=True, slots=True)
class TradeProposal:
    strategy: str
    strategy_version: str
    business_key: str
    venue: Venue
    symbol: str
    side: Side
    quantity: Decimal
    limit_price: Decimal | None
    rationale: str


class Strategy(Protocol):
    name: str
    version: str

    def on_tick(self, view: MarketView) -> list[TradeProposal]: ...


class TrialOutcome(StrEnum):
    PASSED = "passed"
    FAILED_AFTER_COSTS = "failed_after_costs"
    INSUFFICIENT_DATA = "insufficient_data"
    REJECTED_BY_REVIEW = "rejected_by_review"
    LEAKAGE_FOUND = "leakage_found"


@dataclass(frozen=True, slots=True)
class StrategySpec:
    """The one-page card an AI proposer submits. Code is written from it, never by it."""

    name: str
    hypothesis: str
    features: tuple[str, ...]
    entry_rule: str
    exit_rule: str
    parameter_ranges: dict[str, tuple[Decimal, Decimal]]
    expected_failure_modes: tuple[str, ...]
    proposer: str


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """One backtest run. Append-only; failures are kept (the falsification registry)."""

    trial_id: str
    spec_name: str
    code_version: str
    parameter_hash: str
    data_window: tuple[int, int]
    outcome: TrialOutcome
    sharpe: Decimal | None
    deflated_sharpe: Decimal | None
    pbo: Decimal | None
    notes: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)
