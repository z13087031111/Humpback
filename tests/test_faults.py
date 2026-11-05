"""End-to-end fault injection against the simulated venue (milestone 1.3 scenarios)."""

from decimal import Decimal
from pathlib import Path

from humpback.core.clock import ManualClock
from humpback.domain.models import Side, TimeInForce, Venue
from humpback.ledger.events import EventKind
from humpback.ledger.orders import OrderStatus
from humpback.risk.supervisor import STARTUP_LOCK, UNKNOWN_ORDERS, TradingMode
from humpback.venues.sim import Fault, SimVenue
from tests.conftest import ACCOUNT, System, build_system

ACCOUNTS = [(Venue.SIM, ACCOUNT)]


def test_happy_path_fill_reconciles_cleanly(system: System) -> None:
    view = system.manager.submit(system.intent(qty="0.05", price="50010"))
    assert view.status is OrderStatus.FILLED
    assert system.ledger.position(Venue.SIM, ACCOUNT, "BTC").quantity == Decimal("0.05")
    report = system.reconciler.check(Venue.SIM, ACCOUNT)
    assert report.clean, report.discrepancies
    assert system.supervisor.mode is TradingMode.NORMAL


def test_dropped_request_stays_unknown_until_proven_absent(system: System) -> None:
    system.sim.inject(Fault.DROP_REQUEST)
    view = system.manager.submit(system.intent(qty="0.05", price="49000"))
    assert view.status is OrderStatus.UNKNOWN_IN_FLIGHT
    assert system.supervisor.mode is TradingMode.REDUCE_ONLY
    assert system.supervisor.has(UNKNOWN_ORDERS)

    # One "not found" is not proof (Hyperliquid can answer unknownOid for a real order).
    report = system.resolver.run()
    assert report.still_unknown == (view.intent.intent_id,)
    system.resolver.run()
    system.resolver.run()
    assert system.ledger.order(view.intent.intent_id).status is OrderStatus.UNKNOWN_IN_FLIGHT
    # Enough checks *and* enough time: now it may be confirmed absent.
    system.clock.advance(30_000)
    report = system.resolver.run()
    assert report.still_unknown == ()
    assert system.ledger.order(view.intent.intent_id).status is OrderStatus.NOT_FOUND_AT_VENUE
    assert system.supervisor.mode is TradingMode.NORMAL


def test_lost_response_is_resolved_from_the_venue(system: System) -> None:
    system.sim.inject(Fault.LOSE_RESPONSE)
    view = system.manager.submit(system.intent(qty="0.05", price="49000"))  # rests on the book
    assert view.status is OrderStatus.UNKNOWN_IN_FLIGHT
    system.resolver.run()
    resolved = system.ledger.order(view.intent.intent_id)
    assert resolved.status is OrderStatus.OPEN
    assert resolved.venue_order_id is not None
    assert not system.supervisor.has(UNKNOWN_ORDERS)
    assert system.reconciler.check(Venue.SIM, ACCOUNT).clean


def test_lost_response_on_a_filled_order_books_the_fill(system: System) -> None:
    system.sim.inject(Fault.LOSE_RESPONSE)
    view = system.manager.submit(system.intent(qty="0.05", price="50010"))
    assert view.status is OrderStatus.UNKNOWN_IN_FLIGHT
    system.resolver.run()
    assert system.ledger.order(view.intent.intent_id).status is OrderStatus.FILLED
    assert system.ledger.position(Venue.SIM, ACCOUNT, "BTC").quantity == Decimal("0.05")


def test_same_decision_is_never_sent_twice(system: System) -> None:
    system.sim.inject(Fault.DROP_REQUEST)
    first = system.manager.submit(system.intent(qty="0.05", price="49000", key="decision-1"))
    again = system.manager.submit(system.intent(qty="0.05", price="49000", key="decision-1"))
    assert again.intent.intent_id == first.intent.intent_id
    assert system.sim.requests_received == 1


def test_duplicate_fill_reports_are_booked_once(system: System) -> None:
    system.sim.inject(Fault.DUPLICATE_FILLS)
    view = system.manager.submit(system.intent(qty="0.05", price="50010"))
    assert view.filled_qty == Decimal("0.05")
    assert system.ledger.position(Venue.SIM, ACCOUNT, "BTC").quantity == Decimal("0.05")
    assert system.reconciler.check(Venue.SIM, ACCOUNT).clean


def test_late_fills_keep_the_order_live_until_they_arrive(system: System) -> None:
    system.sim.inject(Fault.DELAY_FILLS, sticky=True)
    view = system.manager.submit(system.intent(qty="0.05", price="50010"))
    assert view.status is OrderStatus.OPEN and view.is_live  # accepted, fills not seen yet
    refreshed = system.manager.refresh_order(view.intent.intent_id)
    assert refreshed.status is OrderStatus.OPEN  # venue says filled; we wait for the fills
    report = system.reconciler.check(Venue.SIM, ACCOUNT)
    assert not report.clean  # position differs: halts new orders
    assert system.supervisor.mode is TradingMode.HALT_NEW_ORDERS
    system.sim.clear_faults()
    system.sim.release_delayed_fills()
    assert system.reconciler.check(Venue.SIM, ACCOUNT).clean
    assert system.ledger.order(view.intent.intent_id).status is OrderStatus.FILLED
    assert system.supervisor.mode is TradingMode.NORMAL


def test_ioc_remainder_expires_and_partial_fill_is_kept(system: System) -> None:
    view = system.manager.submit(
        system.intent(qty="0.08", price="50010", tif=TimeInForce.IOC, symbol="BTC")
    )
    # Only 1 BTC rests at 50010, so the whole 0.08 fills; use a thinner book for a partial.
    assert view.status is OrderStatus.FILLED
    system.sim.set_book("ETH", bids=[("2999", "10")], asks=[("3001", "0.5")])
    partial = system.manager.submit(
        system.intent(symbol="ETH", qty="1", price="3001", tif=TimeInForce.IOC)
    )
    assert partial.status is OrderStatus.EXPIRED
    assert partial.filled_qty == Decimal("0.5")


def test_cancel_after_fill_reads_the_true_state(system: System) -> None:
    resting = system.manager.submit(system.intent(qty="0.05", price="49000"))
    assert resting.status is OrderStatus.OPEN
    system.sim.trade_through("BTC", Decimal(49000))
    after = system.manager.cancel(resting.intent.intent_id)
    assert after.status is OrderStatus.FILLED
    assert system.ledger.position(Venue.SIM, ACCOUNT, "BTC").quantity == Decimal("0.05")


def test_venue_reject_is_definitive(system: System) -> None:
    system.sim.inject(Fault.REJECT, "insufficient margin")
    view = system.manager.submit(system.intent(qty="0.05", price="49000"))
    assert view.status is OrderStatus.REJECTED and view.reason == "insufficient margin"
    assert system.supervisor.mode is TradingMode.NORMAL


def test_orphan_order_at_venue_halts_new_orders(system: System) -> None:
    # Someone (or a bug) placed an order Humpback does not know about.
    foreign = build_system(system.path / "other", sim=system.sim, clock=system.clock)
    try:
        foreign.manager.submit(foreign.intent(qty="0.01", price="49000"))
    finally:
        foreign.close()
    report = system.reconciler.check(Venue.SIM, ACCOUNT)
    kinds = {d.kind for d in report.discrepancies}
    assert "orphan_open_order" in kinds
    assert system.supervisor.mode is TradingMode.HALT_NEW_ORDERS
    blocked = system.manager.submit(system.intent(qty="0.01", price="49000"))
    assert blocked.status is OrderStatus.RISK_REJECTED


def test_crash_between_persist_and_send_recovers_on_restart(tmp_path: Path) -> None:
    clock = ManualClock()
    sim = SimVenue(clock)
    s = build_system(tmp_path, sim=sim, clock=clock)
    s.deposit("10000")
    intent = s.intent(qty="0.05", price="50010")
    s.ledger.record_intent(intent, client_id="0x" + "1" * 32, at_ms=clock.now_ms())
    s.ledger.transition(intent.intent_id, EventKind.SUBMIT_STARTED, at_ms=clock.now_ms())
    s.close()  # process dies here: the venue may or may not have the order

    restarted = build_system(tmp_path, sim=sim, clock=clock)
    try:
        from humpback.recon.recovery import startup_recovery

        report = startup_recovery(restarted.resolver, restarted.reconciler, ACCOUNTS)
        assert report.interrupted_submits == 1
        assert not report.unlocked  # still unknown: one not-found is not proof
        assert restarted.supervisor.has(STARTUP_LOCK)
        assert restarted.ledger.order(intent.intent_id).status is OrderStatus.UNKNOWN_IN_FLIGHT
        clock.advance(30_000)
        restarted.resolver.run()
        restarted.resolver.run()
        report = startup_recovery(restarted.resolver, restarted.reconciler, ACCOUNTS)
        assert report.unlocked
        assert restarted.supervisor.mode is TradingMode.NORMAL
        assert restarted.ledger.order(intent.intent_id).status is OrderStatus.NOT_FOUND_AT_VENUE
    finally:
        restarted.close()


def test_restart_rebuilds_state_and_reconciles(tmp_path: Path) -> None:
    clock = ManualClock()
    sim = SimVenue(clock)
    s = build_system(tmp_path, sim=sim, clock=clock)
    s.deposit("10000")
    s.manager.submit(s.intent(qty="0.05", price="50010"))
    s.manager.submit(s.intent(side=Side.SELL, qty="0.02", price="49990"))
    position = s.ledger.position(Venue.SIM, ACCOUNT, "BTC")
    s.close()
    restarted = build_system(tmp_path, sim=sim, clock=clock)
    try:
        from humpback.recon.recovery import startup_recovery

        report = startup_recovery(restarted.resolver, restarted.reconciler, ACCOUNTS)
        assert report.unlocked, report
        assert restarted.ledger.position(Venue.SIM, ACCOUNT, "BTC") == position
    finally:
        restarted.close()
