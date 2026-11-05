"""Periodic reconciliation of the ledger against what each venue reports.

Any difference beyond tolerance halts new orders for that account (``HALT_NEW_ORDERS``)
until a later check comes back clean. Orders still being resolved (``SUBMITTING`` /
``UNKNOWN_IN_FLIGHT``) are the resolver's job and are not counted as mismatches.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from humpback.audit.log import AuditLog
from humpback.core.clock import Clock
from humpback.core.money import ZERO
from humpback.domain.models import Venue
from humpback.engine import OrderManager
from humpback.risk.supervisor import Supervisor, TradingMode, recon_mismatch


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: str
    venue: Venue
    account: str
    subject: str
    ledger_value: str
    venue_value: str


@dataclass(frozen=True, slots=True)
class ReconReport:
    venue: Venue
    account: str
    checked_at_ms: int
    discrepancies: tuple[Discrepancy, ...]

    @property
    def clean(self) -> bool:
        return not self.discrepancies


class Reconciler:
    def __init__(
        self,
        manager: OrderManager,
        supervisor: Supervisor,
        clock: Clock,
        audit: AuditLog,
        *,
        qty_tolerance: Decimal = ZERO,
        collateral_tolerance: Decimal = Decimal("0.01"),
    ) -> None:
        self.manager = manager
        self.ledger = manager.ledger
        self.supervisor = supervisor
        self.clock = clock
        self.audit = audit
        self.qty_tolerance = qty_tolerance
        self.collateral_tolerance = collateral_tolerance

    def check(self, venue: Venue, account: str) -> ReconReport:
        self.manager.sync_fills(venue, account)
        snapshot = self.manager.transports[venue].fetch_account(account)
        found: list[Discrepancy] = []

        remote_positions = {p.symbol: p.quantity for p in snapshot.positions}
        local_positions = {
            s: p.quantity
            for (v, a, s), p in self.ledger.positions().items()
            if v is venue and a == account
        }
        for symbol in sorted(set(remote_positions) | set(local_positions)):
            local = local_positions.get(symbol, ZERO)
            remote = remote_positions.get(symbol, ZERO)
            if abs(local - remote) > self.qty_tolerance:
                found.append(
                    Discrepancy("position", venue, account, symbol, str(local), str(remote))
                )

        remote_open = {o.client_id for o in snapshot.open_orders}
        for view in self.ledger.live_orders(venue):
            if view.intent.account != account or view.needs_resolution:
                continue
            if view.client_id not in remote_open:
                found.append(
                    Discrepancy(
                        "missing_open_order",
                        venue,
                        account,
                        view.client_id,
                        view.status.value,
                        "absent",
                    )
                )
        for client_id in sorted(remote_open):
            known = self.ledger.order_by_client_id(client_id)
            if known is None:
                found.append(
                    Discrepancy("orphan_open_order", venue, account, client_id, "absent", "open")
                )
            elif not known.is_live:
                found.append(
                    Discrepancy(
                        "open_order_status", venue, account, client_id, known.status.value, "open"
                    )
                )

        local_collateral = self.ledger.collateral(venue, account).total
        if abs(local_collateral - snapshot.collateral) > self.collateral_tolerance:
            found.append(
                Discrepancy(
                    "collateral",
                    venue,
                    account,
                    "total",
                    str(local_collateral),
                    str(snapshot.collateral),
                )
            )

        report = ReconReport(venue, account, self.clock.now_ms(), tuple(found))
        code = recon_mismatch(venue.value, account)
        if report.clean:
            self.supervisor.clear_reason(code)
        else:
            self.supervisor.raise_reason(
                code,
                TradingMode.HALT_NEW_ORDERS,
                f"{len(found)} discrepancies",
                report.checked_at_ms,
            )
            self.audit.record(
                "recon_mismatch",
                "system",
                venue=venue.value,
                account=account,
                discrepancies=[d.kind + ":" + d.subject for d in found],
            )
        return report
