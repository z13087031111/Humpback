"""Regression tests from the independent adversarial review (2026-11-05).

Each test asserts the safe behaviour; all of them failed before the fixes in this commit.
"""

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from humpback.capital.transfer import ArrivalEvidence, EvidenceKind, TransferBook, TransferState
from humpback.core.errors import (
    InvalidTransition,
    LedgerError,
    SignerRefused,
    SignerUnavailable,
)
from humpback.core.money import MoneyError
from humpback.domain.models import OrderIntent, OrderType, Side, TimeInForce, Venue
from humpback.ledger.book import Ledger
from humpback.ledger.orders import OrderStatus
from humpback.ledger.store import EventStore
from humpback.recon.recovery import startup_recovery
from humpback.risk.gate import Approval, MarketContext, Rejection
from humpback.risk.halt import HaltStage, _round_to_rules
from humpback.risk.supervisor import EMERGENCY_STOP, UNKNOWN_ORDERS, TradingMode
from humpback.venues.base import Action
from humpback.venues.precision import HyperliquidRules
from humpback.venues.sim import Fault
from tests.conftest import ACCOUNT, System, build_system, limits

ACCOUNTS = [(Venue.SIM, ACCOUNT)]


# F1: cancelling an UNKNOWN_IN_FLIGHT order removes it from resolution and clears REDUCE_ONLY
def test_cancel_of_unknown_order_keeps_it_unresolved(system: System) -> None:
    system.sim.inject(Fault.DROP_REQUEST)
    view = system.manager.submit(system.intent(qty="0.05", price="49000"))
    assert view.status is OrderStatus.UNKNOWN_IN_FLIGHT
    after = system.manager.cancel(view.intent.intent_id)  # sim: "order not open" -> refresh
    system.resolver.run()
    # Order existence still unproven, yet:
    assert after.status is OrderStatus.CANCEL_REQUESTED
    assert system.supervisor.has(UNKNOWN_ORDERS), "UNKNOWN_ORDERS cleared with no evidence"
    assert system.supervisor.mode is not TradingMode.NORMAL


# F2a: a CANCEL approval (granted in every mode, no risk checks) signs a PLACE body
def test_cancel_approval_cannot_sign_a_new_order(system: System) -> None:
    resting = system.manager.submit(system.intent(qty="0.01", price="49000"))
    system.supervisor.raise_reason("x", TradingMode.HALT_NEW_ORDERS, "halt", 0)
    cancel_req = system.sim.build_cancel(resting.intent, resting.client_id)
    approval = system.gate.approve_cancel(resting, cancel_req)
    assert isinstance(approval, Approval)
    big = system.intent(qty="0.09", price="50010")
    smuggled = replace(
        cancel_req,
        body={
            "op": "place",
            "intent": replace(big, intent_id=resting.intent.intent_id).to_record(),
        },
        client_id=resting.client_id,
    )
    with pytest.raises(SignerRefused):
        signed = system.signer.sign(smuggled, approval, trace_id="t")
        system.sim.send(signed)  # would trade 0.09 BTC while HALT_NEW_ORDERS


# F2b: approval for an intent on venue A signs a request routed to venue B / other account
def test_approval_is_bound_to_venue_and_account(system: System) -> None:
    view, _ = system.ledger.record_intent(system.intent(), client_id="0x" + "a" * 32, at_ms=0)
    req = system.sim.build_place(view.intent, view.client_id)
    approval = system.gate.evaluate_place(view, MarketContext(Decimal(50000)), req)
    assert isinstance(approval, Approval)
    with pytest.raises(SignerRefused):
        system.signer.sign(replace(req, account="0xvictim"), approval, trace_id="t")


# F3: emergency stop does not survive a restart; trading resumes without operator
def test_emergency_stop_survives_restart(tmp_path: Path) -> None:
    s = build_system(tmp_path)
    s.deposit("10000")
    s.halt.trigger("drawdown", ACCOUNTS, close_positions=False, operator="owner")
    sim, clock = s.sim, s.clock
    s.close()
    r = build_system(tmp_path, sim=sim, clock=clock)
    try:
        startup_recovery(r.resolver, r.reconciler, ACCOUNTS)
        assert r.supervisor.has_prefix(EMERGENCY_STOP)
        assert r.supervisor.mode is not TradingMode.NORMAL
    finally:
        r.close()


# F4: one failing cancel aborts the whole emergency stop (no stage 2, later orders untouched)
def test_emergency_stop_continues_past_a_failing_cancel(system: System, monkeypatch) -> None:
    a = system.manager.submit(system.intent(qty="0.01", price="49000"))
    b = system.manager.submit(system.intent(qty="0.01", price="49100"))
    assert a.status is OrderStatus.OPEN and b.status is OrderStatus.OPEN
    real = system.signer.sign

    def flaky(request, approval, *, trace_id):
        if request.action is Action.CANCEL_ORDER and request.intent_id == a.intent.intent_id:
            raise SignerUnavailable("hsm hiccup")
        return real(request, approval, trace_id=trace_id)

    monkeypatch.setattr(system.signer, "sign", flaky)
    halt_id = system.halt.trigger("x", ACCOUNTS, close_positions=True, operator="o")
    assert system.halt.stage(halt_id) is HaltStage.CANCEL_OR_CLOSE_REQUESTED
    assert system.ledger.order(b.intent.intent_id).status is OrderStatus.CANCELED


# F5: emergency close price for Hyperliquid violates HL precision -> PrecisionError -> abort
def test_halt_close_price_respects_hyperliquid_rules() -> None:
    rules = HyperliquidRules(sz_decimals=5)
    for ref in (Decimal("50003"), Decimal("97123.5")):
        price = _round_to_rules(ref * Decimal("0.99"), rules, Side.SELL)
        rules.check_price(price)  # raises: >5 significant figures


# F6: IOC partial with late fills -> EXPIRED immediately, the filled part vanishes from exposure
def test_expired_with_unseen_fills_still_counts_exposure(tmp_path: Path) -> None:
    s = build_system(tmp_path, risk=limits(max_symbol_notional=Decimal(5500)))
    s.deposit("10000")
    try:
        s.sim.set_book("BTC", bids=[("49990", "1")], asks=[("50010", "0.07"), ("50020", "2")])
        s.sim.inject(Fault.DELAY_FILLS, sticky=True)
        ioc = s.manager.submit(s.intent(qty="0.08", price="50010", tif=TimeInForce.IOC))
        assert ioc.status is OrderStatus.EXPIRED and ioc.filled_qty == 0  # 0.07 really filled
        nxt = s.manager.submit(s.intent(qty="0.05", price="49600"))
        # true worst case 0.12 BTC * ~50000 = 6000 > 5500 limit
        assert nxt.status is OrderStatus.RISK_REJECTED
    finally:
        s.close()


# F7: two intents sharing an intent_id: the first silently disappears from exposure
def test_intent_id_collision_is_rejected() -> None:
    ledger = Ledger(EventStore(":memory:"))

    def mk(key: str) -> OrderIntent:
        return OrderIntent(
            "same",
            key,
            ACCOUNT,
            Venue.SIM,
            "BTC",
            Side.BUY,
            OrderType.LIMIT,
            Decimal(1),
            0,
            "t",
            limit_price=Decimal(100),
        )

    ledger.record_intent(mk("k1"), client_id="c1", at_ms=0)
    from humpback.ledger.events import EventKind

    ledger.transition("same", EventKind.SUBMIT_STARTED, at_ms=0)
    with pytest.raises(LedgerError, match="already used"):
        ledger.record_intent(mk("k2"), client_id="c2", at_ms=0)


# F8: one arrival proof releases the lock of two different transfers
def test_arrival_evidence_cannot_be_reused(system: System) -> None:
    book = TransferBook(system.ledger, system.clock)
    src, dst = (Venue.SIM, ACCOUNT), (Venue.HYPERLIQUID, ACCOUNT)
    ev = ArrivalEvidence(EvidenceKind.CHAIN_TX_CONFIRMED, "0xsame", Decimal(100), 1)
    ids = []
    for _ in range(2):
        t = book.initiate(src, dst, Decimal(100), trace_id="t")
        book.advance(t.transfer_id, TransferState.SUBMITTED, trace_id="t")
        ids.append(t.transfer_id)
    book.advance(ids[0], TransferState.FUNDS_ACTUALLY_CREDITED, trace_id="t", evidence=ev)
    with pytest.raises(InvalidTransition, match="evidence already used"):
        book.advance(ids[1], TransferState.FUNDS_ACTUALLY_CREDITED, trace_id="t", evidence=ev)


# F9: gate re-approves an already-sent order; signer signs a second place for the same cloid
def test_gate_refuses_to_reapprove_a_sent_order(system: System) -> None:
    filled = system.manager.submit(system.intent(qty="0.01", price="50010"))
    assert filled.status is OrderStatus.FILLED
    req = system.sim.build_place(filled.intent, filled.client_id)
    again = system.gate.evaluate_place(filled, MarketContext(Decimal(50000)), req)
    assert not isinstance(again, Approval)


# F10: a FULL fill after NOT_FOUND_CONFIRMED is silently accepted (partial fill raises)
def test_full_fill_after_not_found_is_a_contradiction() -> None:
    from humpback.core.errors import InvalidTransition
    from humpback.domain.models import Fill
    from humpback.ledger.events import EventKind

    ledger = Ledger(EventStore(":memory:"))
    it = OrderIntent(
        "i1",
        "k1",
        ACCOUNT,
        Venue.SIM,
        "BTC",
        Side.BUY,
        OrderType.LIMIT,
        Decimal(1),
        0,
        "t",
        limit_price=Decimal(100),
    )
    ledger.record_intent(it, client_id="c1", at_ms=0)
    ledger.transition("i1", EventKind.SUBMIT_STARTED, at_ms=0)
    ledger.transition("i1", EventKind.OUTCOME_UNKNOWN, {"cause": "x"}, at_ms=0)
    ledger.transition("i1", EventKind.NOT_FOUND_CONFIRMED, {"evidence": "3x"}, at_ms=1)
    f = Fill(
        Venue.SIM,
        "f1",
        "i1",
        ACCOUNT,
        "BTC",
        Side.BUY,
        Decimal(1),
        Decimal(100),
        Decimal(0),
        "USDC",
        False,
        2,
    )
    with pytest.raises(InvalidTransition):
        ledger.record_fill(f, at_ms=3)


# F11/F14: more than 28 significant digits used to round silently; now refused at the boundary
def test_overlong_decimals_are_refused() -> None:
    q = Decimal("0.10000000000000000000000000001")  # 29 significant digits
    with pytest.raises(MoneyError, match="significant digits"):
        OrderIntent(
            "i1",
            "k1",
            ACCOUNT,
            Venue.SIM,
            "BTC",
            Side.BUY,
            OrderType.LIMIT,
            q,
            0,
            "t",
            limit_price=Decimal(100),
        )


# F12: fill for a different account/venue than the intent is booked to the wrong position key
def test_fill_must_match_intent_account() -> None:
    from humpback.core.errors import InvalidTransition
    from humpback.domain.models import Fill
    from humpback.ledger.events import EventKind

    ledger = Ledger(EventStore(":memory:"))
    it = OrderIntent(
        "i1",
        "k1",
        ACCOUNT,
        Venue.SIM,
        "BTC",
        Side.BUY,
        OrderType.LIMIT,
        Decimal(1),
        0,
        "t",
        limit_price=Decimal(100),
    )
    ledger.record_intent(it, client_id="c1", at_ms=0)
    ledger.transition("i1", EventKind.SUBMIT_STARTED, at_ms=0)
    f = Fill(
        Venue.HYPERLIQUID,
        "f1",
        "i1",
        "0xother",
        "BTC",
        Side.BUY,
        Decimal(1),
        Decimal(100),
        Decimal(0),
        "USDC",
        False,
        2,
    )
    with pytest.raises(InvalidTransition):
        ledger.record_fill(f, at_ms=3)


# F13: resuming one halt clears EMERGENCY_STOP while another halt is still unconfirmed
def test_resume_of_one_halt_does_not_lift_another(system: System) -> None:
    h1 = system.halt.trigger("a", ACCOUNTS, close_positions=False, operator="o")
    h2 = system.halt.trigger("b", ACCOUNTS, close_positions=False, operator="o")
    assert system.halt.confirm(h1).stage is HaltStage.VENUE_EXECUTION_CONFIRMED
    system.halt.resume(h1, "o")
    assert system.halt.stage(h2) is HaltStage.CANCEL_OR_CLOSE_REQUESTED
    assert system.supervisor.has_prefix(EMERGENCY_STOP)


# F15: huge limit price -> quantize InvalidOperation escapes the gate after the intent is recorded
def test_gate_survives_extreme_deviation(system: System) -> None:
    view, _ = system.ledger.record_intent(
        system.intent(price="1" + "0" * 30, qty="0.001"), client_id="0x" + "b" * 32, at_ms=0
    )
    req = system.sim.build_place(view.intent, view.client_id)
    decision = system.gate.evaluate_place(view, MarketContext(Decimal(1)), req)
    assert isinstance(decision, Rejection)


# F16: an ACKed order (has venue_order_id) must not be closed as NOT_FOUND after a cancel timeout
def test_acked_order_cannot_be_confirmed_absent() -> None:
    from humpback.core.errors import InvalidTransition
    from humpback.ledger.events import EventKind

    ledger = Ledger(EventStore(":memory:"))
    it = OrderIntent(
        "i1",
        "k1",
        ACCOUNT,
        Venue.SIM,
        "BTC",
        Side.BUY,
        OrderType.LIMIT,
        Decimal(1),
        0,
        "t",
        limit_price=Decimal(100),
    )
    ledger.record_intent(it, client_id="c1", at_ms=0)
    ledger.transition("i1", EventKind.SUBMIT_STARTED, at_ms=0)
    ledger.transition("i1", EventKind.VENUE_ACCEPTED, {"venue_order_id": "77"}, at_ms=0)
    ledger.transition("i1", EventKind.CANCEL_REQUESTED, at_ms=1)
    ledger.transition("i1", EventKind.OUTCOME_UNKNOWN, {"cause": "cancel timeout"}, at_ms=1)
    with pytest.raises(InvalidTransition):
        ledger.transition("i1", EventKind.NOT_FOUND_CONFIRMED, {"evidence": "3x"}, at_ms=99)


# F17: one agent key used on Hyperliquid (ms nonces) and Aster (us nonces) bricks HL signing
def test_nonce_units_do_not_poison_each_other(tmp_path: Path) -> None:
    from humpback.core.clock import ManualClock
    from humpback.signer.nonces import NonceAllocator

    n = NonceAllocator(tmp_path / "n.db", ManualClock())
    n.next("0xAgent", "us")
    n.next("0xagent", "ms")  # raises NONCE_AHEAD_OF_CLOCK


# F6b: emergency close for two accounts on the same venue and symbol must close both
def test_emergency_close_covers_several_accounts(system: System) -> None:
    other = "0xaaaa000000000000000000000000000000000009"
    system.manager.submit(system.intent(qty="0.02", price="50010"))
    second = replace(system.intent(qty="0.02", price="50010"), account=other)
    assert system.manager.submit(second).status is OrderStatus.FILLED
    accounts = [(Venue.SIM, ACCOUNT), (Venue.SIM, other)]
    halt_id = system.halt.trigger("x", accounts, close_positions=True, operator="o")
    for account in (ACCOUNT, other):
        assert system.ledger.position(Venue.SIM, account, "BTC").quantity == 0
    requests = system.ledger.events_for(f"halt:{halt_id}")[1].payload["requests"]
    assert not any("failed" in r for r in requests), requests
