"""Resolve ``UNKNOWN_IN_FLIGHT`` orders by asking the venue, never by guessing.

An unknown order becomes terminal only on evidence:

* the venue reports a definite status (open, filled, cancelled, rejected, expired), or
* the venue says "not found" repeatedly (``required_checks`` times) and the order is older
  than ``grace_ms``. Hyperliquid can return ``unknownOid`` for an order that was in fact
  placed, so a single not-found is never accepted as proof.

While any order is unresolved the supervisor keeps ``UNKNOWN_ORDERS`` raised (reduce-only).
"""

from __future__ import annotations

from dataclasses import dataclass

from humpback.audit.log import AuditLog
from humpback.core.clock import Clock
from humpback.core.errors import InvalidTransition
from humpback.engine import OrderManager
from humpback.ledger.events import EventKind
from humpback.ledger.orders import OrderStatus, OrderView
from humpback.risk.supervisor import UNKNOWN_ORDERS, Supervisor, TradingMode, recon_mismatch


@dataclass(frozen=True, slots=True)
class ResolutionReport:
    checked: int
    resolved: int
    still_unknown: tuple[str, ...]


class UnknownOrderResolver:
    def __init__(
        self,
        manager: OrderManager,
        supervisor: Supervisor,
        clock: Clock,
        audit: AuditLog,
        *,
        grace_ms: int = 60_000,
        required_checks: int = 3,
    ) -> None:
        self.manager = manager
        self.ledger = manager.ledger
        self.supervisor = supervisor
        self.clock = clock
        self.audit = audit
        self.grace_ms = grace_ms
        self.required_checks = required_checks

    def run(self) -> ResolutionReport:
        pending = self.ledger.unresolved_orders()
        resolved = 0
        for view in pending:
            intent = view.intent
            try:
                after = self.manager.refresh_order(intent.intent_id)
            except Exception as exc:  # the query itself failed: still unknown
                self.audit.record(
                    "resolve_query_failed",
                    intent.trace_id,
                    intent_id=intent.intent_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if after.status is OrderStatus.UNKNOWN_IN_FLIGHT and self._may_confirm_absent(after):
                try:
                    after = self.ledger.transition(
                        intent.intent_id,
                        EventKind.NOT_FOUND_CONFIRMED,
                        {
                            "evidence": f"not found {after.not_found_checks}x over "
                            f"{self.clock.now_ms() - (after.unknown_since_ms or 0)}ms"
                        },
                        at_ms=self.clock.now_ms(),
                    )
                except InvalidTransition as exc:
                    self.supervisor.raise_reason(
                        recon_mismatch(intent.venue.value, intent.account),
                        TradingMode.HALT_NEW_ORDERS,
                        str(exc),
                        self.clock.now_ms(),
                    )
            if after.status not in (OrderStatus.UNKNOWN_IN_FLIGHT, OrderStatus.SUBMITTING):
                resolved += 1
                self.audit.record(
                    "unknown_resolved",
                    intent.trace_id,
                    intent_id=intent.intent_id,
                    status=after.status.value,
                )
        remaining = tuple(o.intent.intent_id for o in self.ledger.unresolved_orders())
        if remaining:
            self.supervisor.raise_reason(
                UNKNOWN_ORDERS,
                TradingMode.REDUCE_ONLY,
                f"{len(remaining)} unresolved",
                self.clock.now_ms(),
            )
        else:
            self.supervisor.clear_reason(UNKNOWN_ORDERS)
        return ResolutionReport(len(pending), resolved, remaining)

    def _may_confirm_absent(self, view: OrderView) -> bool:
        if view.not_found_checks < self.required_checks or view.unknown_since_ms is None:
            return False
        return self.clock.now_ms() - view.unknown_since_ms >= self.grace_ms
