from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from humpback.core.clock import ManualClock
from humpback.core.errors import SignerRefused
from humpback.domain.models import Side, Venue
from humpback.ledger.events import EventKind
from humpback.ledger.orders import OrderStatus
from humpback.risk.gate import Approval, MarketContext, Rejection
from humpback.risk.supervisor import TradingMode
from humpback.signer.backends import TestBackend
from humpback.signer.nonces import NonceAllocator
from humpback.signer.service import SignerService
from humpback.venues.base import Action
from tests.conftest import ACCOUNT, APPROVAL_KEY, System, build_system, limits


def record(system: System, **kw: object):  # type: ignore[no-untyped-def]
    intent = system.intent(**kw)  # type: ignore[arg-type]
    view, _ = system.ledger.record_intent(
        intent, client_id="0x" + intent.intent_id.rjust(32, "0"), at_ms=system.clock.now_ms()
    )
    return view


def evaluate(system: System, view, market: MarketContext):  # type: ignore[no-untyped-def]
    request = system.sim.build_place(view.intent, view.client_id)
    return system.gate.evaluate_place(view, market, request)


def ref(price: str = "50000") -> MarketContext:
    return MarketContext(Decimal(price))


# ------------------------------------------------------------------ gate


def test_gate_approves_a_normal_order(system: System) -> None:
    assert isinstance(evaluate(system, record(system), ref()), Approval)


@pytest.mark.parametrize(
    ("kwargs", "market", "reason"),
    [
        ({"symbol": "DOGE"}, ref(), "symbol_not_allowed"),
        ({}, MarketContext(None), "no_reference_price"),
        ({"price": "52000"}, ref(), "limit_price_deviation_400bps"),
        ({"qty": "0.2"}, ref(), "order_notional_limit"),
        ({"reduce_only": True, "side": Side.SELL}, ref(), "reduce_only_exceeds_position"),
    ],
)
def test_gate_rejections(
    system: System, kwargs: dict[str, object], market: MarketContext, reason: str
) -> None:
    decision = evaluate(system, record(system, **kwargs), market)
    assert isinstance(decision, Rejection)
    assert reason in decision.reasons


def test_unknown_orders_count_as_exposure(system: System) -> None:
    # Three orders of 0.08 BTC (~4,000 each) are fine while nothing is live...
    first = record(system, qty="0.08")
    system.ledger.transition(first.intent.intent_id, EventKind.SUBMIT_STARTED, at_ms=1)
    system.ledger.transition(
        first.intent.intent_id, EventKind.OUTCOME_UNKNOWN, {"cause": "timeout"}, at_ms=2
    )
    second = record(system, qty="0.08")
    system.ledger.transition(second.intent.intent_id, EventKind.SUBMIT_STARTED, at_ms=3)
    system.ledger.transition(
        second.intent.intent_id, EventKind.OUTCOME_UNKNOWN, {"cause": "timeout"}, at_ms=4
    )
    # ...but with two unknown orders in flight a third would breach the 10,000 symbol limit.
    decision = evaluate(system, record(system, qty="0.08"), ref())
    assert isinstance(decision, Rejection)
    assert "symbol_exposure_limit" in decision.reasons


def test_reduce_only_mode_allows_only_reducing_orders(system: System) -> None:
    system.manager.submit(system.intent(qty="0.05", price="50010"))
    assert system.ledger.position(Venue.SIM, ACCOUNT, "BTC").quantity == Decimal("0.05")
    system.supervisor.raise_reason("x", TradingMode.REDUCE_ONLY, "test", 0)
    blocked = evaluate(system, record(system, qty="0.01"), ref())
    assert isinstance(blocked, Rejection) and "mode_reduce_only_blocks_new_risk" in blocked.reasons
    reduce = record(system, qty="0.05", side=Side.SELL, reduce_only=True, price="49990")
    assert isinstance(evaluate(system, reduce, ref()), Approval)
    system.supervisor.raise_reason("y", TradingMode.HALTED, "manual", 0)
    assert isinstance(evaluate(system, reduce, ref()), Rejection)


def test_order_rate_limit(tmp_path: Path) -> None:
    s = build_system(tmp_path, risk=limits(max_orders_per_minute=2))
    try:
        assert isinstance(evaluate(s, record(s), ref()), Approval)
        assert isinstance(evaluate(s, record(s), ref()), Approval)
        third = evaluate(s, record(s), ref())
        assert isinstance(third, Rejection) and "order_rate_limit" in third.reasons
        s.clock.advance(60_000)
        assert isinstance(evaluate(s, record(s), ref()), Approval)
    finally:
        s.close()


def test_daily_loss_limit_switches_to_reduce_only(system: System) -> None:
    system.ledger.record_funding(
        Venue.SIM,
        ACCOUNT,
        "BTC",
        Decimal(-600),
        venue_event_id="f1",
        at_ms=system.clock.now_ms(),
        trace_id="t",
    )
    decision = evaluate(system, record(system), ref())
    assert isinstance(decision, Rejection) and "daily_loss_limit" in decision.reasons
    assert system.supervisor.mode is TradingMode.REDUCE_ONLY


# ------------------------------------------------------------------ signer


def test_signer_needs_a_matching_fresh_unused_approval(system: System) -> None:
    view = record(system)
    approval = evaluate(system, view, ref())
    assert isinstance(approval, Approval)
    request = system.sim.build_place(view.intent, view.client_id)

    forged = replace(approval, mac="0" * 64)
    with pytest.raises(SignerRefused, match="MAC"):
        system.signer.sign(request, forged, trace_id="t")
    other = record(system)
    wrong = replace(request, intent_hash=other.intent.content_hash())
    with pytest.raises(SignerRefused, match="does not match"):
        system.signer.sign(wrong, approval, trace_id="t")

    system.signer.sign(request, approval, trace_id="t")
    with pytest.raises(SignerRefused, match="already used"):
        system.signer.sign(request, approval, trace_id="t")

    late = evaluate(system, record(system), ref())
    assert isinstance(late, Approval)
    system.clock.advance(10_000)
    with pytest.raises(SignerRefused, match="expired"):
        system.signer.sign(request, late, trace_id="t")
    refused = [e.payload["code"] for e in system.signer_audit.entries("signer_refused")]
    assert refused == [
        "APPROVAL_INVALID",
        "APPROVAL_MISMATCH",
        "APPROVAL_REPLAY",
        "APPROVAL_EXPIRED",
    ]


def test_signer_never_signs_withdrawals(system: System) -> None:
    view = record(system)
    approval = evaluate(system, view, ref())
    assert isinstance(approval, Approval)
    request = replace(system.sim.build_place(view.intent, view.client_id), action=Action.WITHDRAW)
    with pytest.raises(SignerRefused, match="not whitelisted"):
        system.signer.sign(request, replace(approval, action=Action.WITHDRAW), trace_id="t")
    with pytest.raises(ValueError, match="never whitelist"):
        SignerService(
            approval_key=APPROVAL_KEY,
            clock=system.clock,
            nonces=system.nonces,
            backends={},
            nonce_units={},
            audit=system.signer_audit,
            allowed_actions=frozenset({Action.PLACE_ORDER, Action.WITHDRAW}),
        )


def test_nonces_are_monotonic_per_signer_and_survive_restart(tmp_path: Path) -> None:
    clock = ManualClock()
    nonces = NonceAllocator(tmp_path / "n.sqlite3", clock)
    a = [nonces.next("0xA", "ms") for _ in range(3)]
    assert a == [clock.now_ms(), clock.now_ms() + 1, clock.now_ms() + 2]
    assert nonces.next("0xb", "us") == clock.now_ms() * 1000  # signers are independent
    nonces.close()
    reopened = NonceAllocator(tmp_path / "n.sqlite3", clock)
    assert reopened.next("0xa", "ms") == a[-1] + 1  # case-insensitive, never reused
    reopened.close()


def test_test_backend_is_marked_as_such() -> None:
    backend = TestBackend(Venue.SIM, "0xABC")
    assert backend.address == "0xabc"


def test_risk_rejected_orders_never_reach_the_venue(system: System) -> None:
    view = system.manager.submit(system.intent(qty="1"))  # 50,000 notional > 5,000 limit
    assert view.status is OrderStatus.RISK_REJECTED
    assert system.sim.requests_received == 0
