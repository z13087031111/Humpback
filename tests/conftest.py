"""Shared harness: a complete Humpback stack wired to the simulated venue."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from humpback.audit.log import AuditLog
from humpback.core.clock import ManualClock
from humpback.core.ids import new_trace_id
from humpback.domain.models import OrderIntent, OrderType, Side, TimeInForce, Venue
from humpback.engine import OrderManager, TransportMarketData
from humpback.ledger.book import Ledger
from humpback.ledger.store import EventStore
from humpback.recon.reconciler import Reconciler
from humpback.recon.resolver import UnknownOrderResolver
from humpback.risk.gate import RiskGate
from humpback.risk.halt import EmergencyStop
from humpback.risk.limits import RiskLimits
from humpback.risk.supervisor import Supervisor
from humpback.signer.backends import TestBackend
from humpback.signer.nonces import NonceAllocator
from humpback.signer.service import SignerService
from humpback.venues.sim import SimVenue

ACCOUNT = "0xaaaa000000000000000000000000000000000001"
AGENT = "0xbbbb000000000000000000000000000000000002"
APPROVAL_KEY = b"k" * 32
_counter = itertools.count()


def limits(**overrides: object) -> RiskLimits:
    base: dict[str, object] = {
        "max_order_notional": Decimal(5_000),
        "max_symbol_notional": Decimal(10_000),
        "max_gross_notional": Decimal(20_000),
        "max_daily_loss": Decimal(500),
        "max_orders_per_minute": 30,
        "max_price_deviation_bps": Decimal(200),
        "allowed_symbols": frozenset({("sim", "BTC"), ("sim", "ETH")}),
    }
    base.update(overrides)
    return RiskLimits(**base)  # type: ignore[arg-type]


@dataclass
class System:
    clock: ManualClock
    store: EventStore
    ledger: Ledger
    supervisor: Supervisor
    sim: SimVenue
    gate: RiskGate
    signer: SignerService
    manager: OrderManager
    resolver: UnknownOrderResolver
    reconciler: Reconciler
    halt: EmergencyStop
    audit: AuditLog
    signer_audit: AuditLog
    nonces: NonceAllocator
    path: Path

    def intent(
        self,
        *,
        side: Side = Side.BUY,
        qty: str = "0.1",
        price: str | None = "50000",
        symbol: str = "BTC",
        order_type: OrderType = OrderType.LIMIT,
        tif: TimeInForce = TimeInForce.GTC,
        reduce_only: bool = False,
        key: str | None = None,
    ) -> OrderIntent:
        n = next(_counter)
        return OrderIntent(
            intent_id=f"i{n}",
            business_key=key or f"test:{n}",
            account=ACCOUNT,
            venue=Venue.SIM,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=Decimal(qty),
            created_at_ms=self.clock.now_ms(),
            trace_id=new_trace_id(),
            limit_price=None if price is None else Decimal(price),
            time_in_force=tif,
            reduce_only=reduce_only,
        )

    def deposit(self, amount: str) -> None:
        self.sim.deposit(ACCOUNT, Decimal(amount))
        self.ledger.adjust_collateral(
            Venue.SIM,
            ACCOUNT,
            Decimal(amount),
            reason="deposit",
            reference=f"dep{next(_counter)}",
            at_ms=self.clock.now_ms(),
            trace_id="t",
        )

    def close(self) -> None:
        self.store.close()
        self.audit.close()
        self.signer_audit.close()
        self.nonces.close()


def build_system(
    path: Path,
    *,
    risk: RiskLimits | None = None,
    sim: SimVenue | None = None,
    clock: ManualClock | None = None,
) -> System:
    clock = clock or ManualClock()
    store = EventStore(path / "ledger.sqlite3")
    ledger = Ledger(store)
    supervisor = Supervisor()
    sim = sim or SimVenue(clock)
    sim.authorized_signers.add(AGENT)
    if not sim._books:
        sim.set_book(
            "BTC", bids=[("49990", "1"), ("49980", "2")], asks=[("50010", "1"), ("50020", "2")]
        )
        sim.set_book("ETH", bids=[("2999", "10")], asks=[("3001", "10")])
    market = TransportMarketData({Venue.SIM: sim})
    audit = AuditLog(path / "audit.sqlite3", clock)
    signer_audit = AuditLog(path / "signer-audit.sqlite3", clock)
    nonces = NonceAllocator(path / "nonces.sqlite3", clock)
    gate = RiskGate(
        limits=risk or limits(),
        supervisor=supervisor,
        ledger=ledger,
        clock=clock,
        approval_key=APPROVAL_KEY,
        price_lookup=market.price,
    )
    signer = SignerService(
        approval_key=APPROVAL_KEY,
        clock=clock,
        nonces=nonces,
        backends={Venue.SIM: TestBackend(Venue.SIM, AGENT)},
        nonce_units={Venue.SIM: "ms"},
        audit=signer_audit,
    )
    manager = OrderManager(
        ledger=ledger,
        gate=gate,
        signer=signer,
        codecs={Venue.SIM: sim},
        transports={Venue.SIM: sim},
        supervisor=supervisor,
        market=market,
        clock=clock,
        audit=audit,
    )
    resolver = UnknownOrderResolver(
        manager, supervisor, clock, audit, grace_ms=30_000, required_checks=3
    )
    reconciler = Reconciler(manager, supervisor, clock, audit)
    halt = EmergencyStop(manager, reconciler)
    return System(
        clock,
        store,
        ledger,
        supervisor,
        sim,
        gate,
        signer,
        manager,
        resolver,
        reconciler,
        halt,
        audit,
        signer_audit,
        nonces,
        path,
    )


@pytest.fixture
def system(tmp_path: Path) -> Iterator[System]:
    s = build_system(tmp_path)
    s.deposit("10000")
    yield s
    s.close()
