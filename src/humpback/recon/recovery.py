"""Startup recovery: nothing trades until the ledger agrees with every venue.

1. Raise ``STARTUP_LOCK`` (halt new orders; cancels and closes are still possible).
2. Orders left in ``SUBMITTING`` by a crash become ``UNKNOWN_IN_FLIGHT``: the request may
   have reached the venue after the process died.
3. Pull fills, resolve unknown orders, reconcile every account.
4. Lift the lock only if all of that is clean.
"""

from __future__ import annotations

from dataclasses import dataclass

from humpback.domain.models import Venue
from humpback.ledger.events import EventKind
from humpback.ledger.orders import OrderStatus
from humpback.recon.reconciler import Reconciler, ReconReport
from humpback.recon.resolver import ResolutionReport, UnknownOrderResolver
from humpback.risk.supervisor import STARTUP_LOCK, TradingMode


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    unlocked: bool
    interrupted_submits: int
    resolution: ResolutionReport
    reconciliation: tuple[ReconReport, ...]


def startup_recovery(
    resolver: UnknownOrderResolver, reconciler: Reconciler, accounts: list[tuple[Venue, str]]
) -> RecoveryReport:
    supervisor = resolver.supervisor
    ledger = resolver.ledger
    clock = resolver.clock
    supervisor.raise_reason(
        STARTUP_LOCK, TradingMode.HALT_NEW_ORDERS, "startup recovery", clock.now_ms()
    )
    interrupted = 0
    for view in ledger.orders():
        if view.status is OrderStatus.SUBMITTING:
            ledger.transition(
                view.intent.intent_id,
                EventKind.OUTCOME_UNKNOWN,
                {"cause": "process restarted during submit"},
                at_ms=clock.now_ms(),
            )
            interrupted += 1
    for venue, account in accounts:
        resolver.manager.sync_fills(venue, account)
    resolution = resolver.run()
    reports = tuple(reconciler.check(venue, account) for venue, account in accounts)
    unlocked = not resolution.still_unknown and all(r.clean for r in reports)
    if unlocked:
        supervisor.clear_reason(STARTUP_LOCK)
    return RecoveryReport(unlocked, interrupted, resolution, reports)
