import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from humpback.core.errors import InvalidTransition, LedgerError
from humpback.domain.models import Fill, OrderIntent, OrderType, Side, Venue
from humpback.ledger.book import Ledger, Position
from humpback.ledger.events import EventKind
from humpback.ledger.orders import OrderStatus
from humpback.ledger.store import EventStore

ACCT = "0xabc"


def intent(
    n: int = 1, qty: str = "1", side: Side = Side.BUY, key: str | None = None
) -> OrderIntent:
    return OrderIntent(
        intent_id=f"i{n}",
        business_key=key or f"k{n}",
        account=ACCT,
        venue=Venue.SIM,
        symbol="BTC",
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Decimal(qty),
        created_at_ms=0,
        trace_id="t",
        limit_price=Decimal(100),
    )


def fill(
    fid: str, qty: str, price: str = "100", intent_id: str = "i1", side: Side = Side.BUY
) -> Fill:
    return Fill(
        Venue.SIM,
        fid,
        intent_id,
        ACCT,
        "BTC",
        side,
        Decimal(qty),
        Decimal(price),
        Decimal("0.01"),
        "USDC",
        False,
        1,
    )


@pytest.fixture
def ledger() -> Ledger:
    return Ledger(EventStore(":memory:"))


def test_events_cannot_be_updated_or_deleted(ledger: Ledger) -> None:
    ledger.record_intent(intent(), client_id="c1", at_ms=0)
    db: sqlite3.Connection = ledger.store.raw_connection_for_tests()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE events SET kind = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM events")


def test_second_process_cannot_open_the_same_ledger(tmp_path: Path) -> None:
    first = EventStore(tmp_path / "l.sqlite3")
    with pytest.raises(LedgerError, match="already open"):
        EventStore(tmp_path / "l.sqlite3")
    first.close()
    EventStore(tmp_path / "l.sqlite3").close()


def test_business_key_is_idempotent_and_content_bound(ledger: Ledger) -> None:
    view, created = ledger.record_intent(intent(), client_id="c1", at_ms=0)
    assert created
    again, created = ledger.record_intent(intent(), client_id="c1", at_ms=0)
    assert not created and again == view
    with pytest.raises(LedgerError, match="new content"):
        ledger.record_intent(intent(qty="2"), client_id="c1", at_ms=0)


def test_duplicate_fill_is_recorded_once(ledger: Ledger) -> None:
    ledger.record_intent(intent(qty="1"), client_id="c1", at_ms=0)
    ledger.transition("i1", EventKind.SUBMIT_STARTED, at_ms=1)
    ledger.transition("i1", EventKind.VENUE_ACCEPTED, {"venue_order_id": "9"}, at_ms=2)
    assert ledger.record_fill(fill("f1", "0.4"), at_ms=3)
    assert not ledger.record_fill(fill("f1", "0.4"), at_ms=4)
    assert ledger.order("i1").filled_qty == Decimal("0.4")
    assert ledger.order("i1").status is OrderStatus.PARTIALLY_FILLED


def test_unknown_outcome_never_reads_as_no_order(ledger: Ledger) -> None:
    ledger.record_intent(intent(), client_id="c1", at_ms=0)
    ledger.transition("i1", EventKind.SUBMIT_STARTED, at_ms=1)
    view = ledger.transition("i1", EventKind.OUTCOME_UNKNOWN, {"cause": "timeout"}, at_ms=2)
    assert view.status is OrderStatus.UNKNOWN_IN_FLIGHT
    assert view.is_live  # still counts as exposure
    # A fill proves the order existed.
    ledger.record_fill(fill("f1", "0.5"), at_ms=3)
    assert ledger.order("i1").status is OrderStatus.PARTIALLY_FILLED


def test_late_fill_after_cancel_is_applied_without_reopening(ledger: Ledger) -> None:
    ledger.record_intent(intent(), client_id="c1", at_ms=0)
    for kind in (
        EventKind.SUBMIT_STARTED,
        EventKind.VENUE_ACCEPTED,
        EventKind.CANCEL_REQUESTED,
        EventKind.CANCEL_CONFIRMED,
    ):
        ledger.transition("i1", kind, at_ms=1)
    ledger.record_fill(fill("f1", "0.3"), at_ms=2)
    view = ledger.order("i1")
    assert view.status is OrderStatus.CANCELED
    assert view.filled_qty == Decimal("0.3")
    assert ledger.position(Venue.SIM, ACCT, "BTC").quantity == Decimal("0.3")


def test_contradictions_are_rejected_before_they_reach_the_store(ledger: Ledger) -> None:
    ledger.record_intent(intent(qty="1"), client_id="c1", at_ms=0)
    ledger.transition("i1", EventKind.SUBMIT_STARTED, at_ms=1)
    ledger.record_fill(fill("f1", "1"), at_ms=2)
    seq = ledger.last_seq
    with pytest.raises(InvalidTransition, match="overfill"):
        ledger.record_fill(fill("f2", "0.1"), at_ms=3)
    with pytest.raises(InvalidTransition):
        ledger.transition("i1", EventKind.VENUE_REJECTED, {"reason": "x"}, at_ms=3)
    assert ledger.last_seq == seq


def test_venue_says_filled_but_fills_missing_keeps_order_live(ledger: Ledger) -> None:
    ledger.record_intent(intent(qty="1"), client_id="c1", at_ms=0)
    ledger.transition("i1", EventKind.SUBMIT_STARTED, at_ms=1)
    ledger.transition("i1", EventKind.OUTCOME_UNKNOWN, {"cause": "x"}, at_ms=2)
    view = ledger.transition(
        "i1", EventKind.VENUE_STATUS_OBSERVED, {"status": "filled", "venue_order_id": "7"}, at_ms=3
    )
    assert view.is_live and view.status is OrderStatus.OPEN


def test_replay_rebuilds_identical_projections(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "l.sqlite3")
    ledger = Ledger(store)
    ledger.record_intent(intent(1, qty="2"), client_id="c1", at_ms=0)
    ledger.transition("i1", EventKind.SUBMIT_STARTED, at_ms=1)
    ledger.record_fill(fill("f1", "2", "100"), at_ms=2)
    ledger.record_intent(intent(2, qty="1", side=Side.SELL), client_id="c2", at_ms=3)
    ledger.transition("i2", EventKind.SUBMIT_STARTED, at_ms=4)
    ledger.record_fill(fill("f2", "1", "110", "i2", Side.SELL), at_ms=5)
    ledger.adjust_collateral(
        Venue.SIM, ACCT, Decimal(1000), reason="deposit", reference="d1", at_ms=6, trace_id="t"
    )
    before = (ledger.positions(), ledger.collateral(Venue.SIM, ACCT), ledger.orders())
    store.close()
    reopened = Ledger(EventStore(tmp_path / "l.sqlite3"))
    assert (reopened.positions(), reopened.collateral(Venue.SIM, ACCT), reopened.orders()) == before
    pos = reopened.position(Venue.SIM, ACCT, "BTC")
    assert pos.quantity == Decimal(1) and pos.realized_pnl == Decimal(10)
    assert reopened.collateral(Venue.SIM, ACCT).total == Decimal(1000) + Decimal(10) - Decimal(
        "0.02"
    )


@pytest.mark.parametrize(
    ("fills", "qty", "avg", "realized"),
    [
        ([("buy", "1", "100"), ("buy", "1", "110")], "2", "105", "0"),
        ([("buy", "2", "100"), ("sell", "1", "120")], "1", "100", "20"),
        ([("buy", "1", "100"), ("sell", "3", "90")], "-2", "90", "-10"),
        ([("sell", "1", "100"), ("buy", "1", "80")], "0", "0", "20"),
    ],
)
def test_average_cost_position_accounting(
    fills: list[tuple[str, str, str]], qty: str, avg: str, realized: str
) -> None:
    position = Position()
    for i, (side, q, px) in enumerate(fills):
        position, _ = position.apply_fill(
            Fill(
                Venue.SIM,
                f"f{i}",
                "i",
                ACCT,
                "BTC",
                Side(side),
                Decimal(q),
                Decimal(px),
                Decimal(0),
                "USDC",
                False,
                0,
            )
        )
    assert position.quantity == Decimal(qty)
    assert position.avg_entry == Decimal(avg)
    assert position.realized_pnl == Decimal(realized)
