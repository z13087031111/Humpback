from decimal import Decimal

import pytest

from humpback.capital.transfer import (
    ArrivalEvidence,
    EvidenceKind,
    TransferBook,
    TransferState,
)
from humpback.core.errors import InvalidTransition
from humpback.domain.models import Venue
from humpback.ledger.orders import OrderStatus
from humpback.risk.halt import HaltStage
from humpback.risk.supervisor import EMERGENCY_STOP, TradingMode
from humpback.venues.sim import Fault
from tests.conftest import ACCOUNT, System

ACCOUNTS = [(Venue.SIM, ACCOUNT)]


# ------------------------------------------------------------------ emergency stop


def test_emergency_stop_records_three_stages(system: System) -> None:
    system.manager.submit(system.intent(qty="0.05", price="50010"))  # long 0.05
    resting = system.manager.submit(system.intent(symbol="ETH", qty="1", price="2960"))
    assert resting.status is OrderStatus.OPEN

    halt_id = system.halt.trigger("drawdown", ACCOUNTS, close_positions=True, operator="owner")
    assert system.supervisor.mode is TradingMode.HALT_NEW_ORDERS
    assert system.halt.stage(halt_id) is HaltStage.CANCEL_OR_CLOSE_REQUESTED
    assert system.ledger.order(resting.intent.intent_id).status is OrderStatus.CANCELED

    status = system.halt.confirm(halt_id)
    assert status.stage is HaltStage.VENUE_EXECUTION_CONFIRMED and status.pending == ()
    assert system.ledger.position(Venue.SIM, ACCOUNT, "BTC").quantity == Decimal(0)
    stages = [e.payload["stage"] for e in system.ledger.events_for(f"halt:{halt_id}")]
    assert stages == ["halt_decided", "cancel_or_close_requested", "venue_execution_confirmed"]

    system.halt.resume(halt_id, operator="owner")
    assert not system.supervisor.has_prefix(EMERGENCY_STOP)


def test_close_request_is_not_a_closed_position(system: System) -> None:
    system.manager.submit(system.intent(qty="0.05", price="50010"))
    system.sim.inject(Fault.DELAY_FILLS, sticky=True)  # the close executes but we do not see it
    halt_id = system.halt.trigger("test", ACCOUNTS, close_positions=True, operator="owner")
    status = system.halt.confirm(halt_id)
    assert status.stage is HaltStage.CANCEL_OR_CLOSE_REQUESTED
    assert any(p.startswith("position:BTC") for p in status.pending)
    with pytest.raises(RuntimeError, match="cannot resume"):
        system.halt.resume(halt_id, operator="owner")

    system.sim.clear_faults()
    system.sim.release_delayed_fills()
    assert system.halt.confirm(halt_id).stage is HaltStage.VENUE_EXECUTION_CONFIRMED


# ------------------------------------------------------------------ capital transfers


def evidence(amount: str, ref: str = "0xtx") -> ArrivalEvidence:
    return ArrivalEvidence(EvidenceKind.CHAIN_TX_CONFIRMED, ref, Decimal(amount), 1)


def test_funds_stay_locked_until_arrival_is_proven(system: System) -> None:
    book = TransferBook(system.ledger, system.clock)
    src, dst = (Venue.SIM, ACCOUNT), (Venue.HYPERLIQUID, ACCOUNT)
    t = book.initiate(src, dst, Decimal(1000), trace_id="t")
    assert system.ledger.collateral(*src).in_flight_locked == Decimal(1000)
    assert system.ledger.collateral(*src).free == Decimal(9000)

    for state in (
        TransferState.SUBMITTED,
        TransferState.TIMEOUT_DETECTED,
        TransferState.REFUND_ELIGIBLE,
    ):
        book.advance(t.transfer_id, state, trace_id="t")
        assert system.ledger.collateral(*src).in_flight_locked == Decimal(1000)
    with pytest.raises(InvalidTransition, match="tx hash"):
        book.advance(t.transfer_id, TransferState.REFUND_EXECUTED_PENDING_CONFIRM, trace_id="t")
    book.advance(
        t.transfer_id,
        TransferState.REFUND_EXECUTED_PENDING_CONFIRM,
        trace_id="t",
        reference="0xrefund",
    )
    assert system.ledger.collateral(*src).in_flight_locked == Decimal(1000)
    with pytest.raises(InvalidTransition, match="requires arrival evidence"):
        book.advance(t.transfer_id, TransferState.REFUND_CREDITED, trace_id="t")

    book.advance(
        t.transfer_id,
        TransferState.REFUND_CREDITED,
        trace_id="t",
        evidence=evidence("997.5", "0xrefund"),
    )
    after = system.ledger.collateral(*src)
    assert after.in_flight_locked == Decimal(0)
    assert after.total == Decimal("9997.5")  # the 2.5 the refund did not return is a loss


def test_credited_transfer_moves_collateral(system: System) -> None:
    book = TransferBook(system.ledger, system.clock)
    src, dst = (Venue.SIM, ACCOUNT), (Venue.HYPERLIQUID, ACCOUNT)
    t = book.initiate(src, dst, Decimal(500), trace_id="t")
    book.advance(t.transfer_id, TransferState.SUBMITTED, trace_id="t", reference="0xtx")
    book.advance(
        t.transfer_id, TransferState.FUNDS_ACTUALLY_CREDITED, trace_id="t", evidence=evidence("499")
    )
    assert system.ledger.collateral(*src).total == Decimal(9500)
    assert system.ledger.collateral(*dst).total == Decimal(499)
    with pytest.raises(InvalidTransition):
        book.advance(t.transfer_id, TransferState.TIMEOUT_DETECTED, trace_id="t")


def test_balance_evidence_must_show_the_increase(system: System) -> None:
    book = TransferBook(system.ledger, system.clock)
    t = book.initiate((Venue.SIM, ACCOUNT), (Venue.LIGHTER, ACCOUNT), Decimal(100), trace_id="t")
    book.advance(t.transfer_id, TransferState.SUBMITTED, trace_id="t")
    weak = ArrivalEvidence(
        EvidenceKind.VENUE_BALANCE_INCREASE,
        "lighter:deposit:9",
        Decimal(100),
        1,
        balance_before=Decimal(50),
        balance_after=Decimal(120),
    )
    with pytest.raises(InvalidTransition, match="smaller than claimed"):
        book.advance(
            t.transfer_id, TransferState.FUNDS_ACTUALLY_CREDITED, trace_id="t", evidence=weak
        )
    with pytest.raises(InvalidTransition, match="exceeds free collateral"):
        book.initiate((Venue.SIM, ACCOUNT), (Venue.LIGHTER, ACCOUNT), Decimal(99_999), trace_id="t")
