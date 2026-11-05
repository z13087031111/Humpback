"""Emergency stop with three separately recorded stages.

    HALT_DECIDED               the decision to stop (who, why, scope)
    CANCEL_OR_CLOSE_REQUESTED  every cancel / reduce-only close actually sent
    VENUE_EXECUTION_CONFIRMED  the venues confirm it: no live orders, positions flat if a
                               close was requested, and reconciliation clean

Sending a close is not closing a position. ``confirm`` refuses to record the last stage until
the ledger *and* the venue agree. The supervisor stays in ``HALT_NEW_ORDERS`` until an
operator resumes after confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from humpback.core.ids import new_id, new_trace_id
from humpback.core.money import BPS, ZERO, quantize_down, quantize_up
from humpback.domain.models import OrderIntent, OrderType, Side, TimeInForce, Venue
from humpback.engine import OrderManager
from humpback.ledger.events import EventKind
from humpback.recon.reconciler import Reconciler
from humpback.risk.supervisor import EMERGENCY_STOP, TradingMode


class HaltStage(StrEnum):
    HALT_DECIDED = "halt_decided"
    CANCEL_OR_CLOSE_REQUESTED = "cancel_or_close_requested"
    VENUE_EXECUTION_CONFIRMED = "venue_execution_confirmed"


@dataclass(frozen=True, slots=True)
class HaltStatus:
    halt_id: str
    stage: HaltStage
    pending: tuple[str, ...]


class EmergencyStop:
    def __init__(
        self,
        manager: OrderManager,
        reconciler: Reconciler,
        *,
        close_slippage_bps: Decimal = Decimal(100),
    ) -> None:
        self.manager = manager
        self.ledger = manager.ledger
        self.supervisor = manager.supervisor
        self.clock = manager.clock
        self.reconciler = reconciler
        self.close_slippage_bps = close_slippage_bps
        self._scope: dict[str, tuple[list[tuple[Venue, str]], bool]] = {}

    def trigger(
        self,
        reason: str,
        accounts: list[tuple[Venue, str]],
        *,
        close_positions: bool,
        operator: str,
    ) -> str:
        halt_id = new_id()
        trace_id = new_trace_id()
        now = self.clock.now_ms()
        self.supervisor.raise_reason(EMERGENCY_STOP, TradingMode.HALT_NEW_ORDERS, reason, now)
        self._stage(
            halt_id,
            HaltStage.HALT_DECIDED,
            trace_id,
            reason=reason,
            operator=operator,
            close_positions=close_positions,
            accounts=[f"{v.value}:{a}" for v, a in accounts],
        )
        self._scope[halt_id] = (accounts, close_positions)

        requested: list[str] = []
        for venue, account in accounts:
            for view in self.ledger.live_orders(venue):
                if view.intent.account == account:
                    self.manager.cancel(view.intent.intent_id)
                    requested.append(f"cancel:{view.intent.intent_id}")
            if close_positions:
                requested.extend(self._close_all(halt_id, venue, account, trace_id))
        self._stage(halt_id, HaltStage.CANCEL_OR_CLOSE_REQUESTED, trace_id, requests=requested)
        return halt_id

    def confirm(self, halt_id: str) -> HaltStatus:
        accounts, close_positions = self._scope[halt_id]
        pending: list[str] = []
        for venue, account in accounts:
            report = self.reconciler.check(venue, account)
            pending.extend(f"recon:{d.kind}:{d.subject}" for d in report.discrepancies)
            pending.extend(
                f"live:{o.intent.intent_id}"
                for o in self.ledger.live_orders(venue)
                if o.intent.account == account
            )
            if close_positions:
                pending.extend(
                    f"position:{s}"
                    for (v, a, s), p in self.ledger.positions().items()
                    if v is venue and a == account and p.quantity != ZERO
                )
        if pending:
            return HaltStatus(halt_id, self.stage(halt_id), tuple(pending))
        self._stage(halt_id, HaltStage.VENUE_EXECUTION_CONFIRMED, new_trace_id())
        return HaltStatus(halt_id, HaltStage.VENUE_EXECUTION_CONFIRMED, ())

    def resume(self, halt_id: str, operator: str) -> None:
        if self.stage(halt_id) is not HaltStage.VENUE_EXECUTION_CONFIRMED:
            raise RuntimeError("cannot resume before the venues confirm the halt")
        self._stage(
            halt_id, HaltStage.VENUE_EXECUTION_CONFIRMED, new_trace_id(), resumed_by=operator
        )
        self.supervisor.clear_reason(EMERGENCY_STOP)

    def stage(self, halt_id: str) -> HaltStage:
        stages = [HaltStage(e.payload["stage"]) for e in self.ledger.events_for(f"halt:{halt_id}")]
        if not stages:
            raise KeyError(halt_id)
        return stages[-1]

    def _stage(self, halt_id: str, stage: HaltStage, trace_id: str, **detail: object) -> None:
        self.ledger.append_raw(
            EventKind.HALT_STAGE,
            f"halt:{halt_id}",
            {"stage": stage.value, **detail},
            at_ms=self.clock.now_ms(),
            trace_id=trace_id,
        )

    def _close_all(self, halt_id: str, venue: Venue, account: str, trace_id: str) -> list[str]:
        out = []
        for (v, a, symbol), position in self.ledger.positions().items():
            if v is not venue or a != account or position.quantity == ZERO:
                continue
            ref = self.manager.market.context(venue, symbol).reference_price
            if ref is None:
                out.append(f"close_skipped_no_price:{symbol}")
                continue
            side = Side.SELL if position.quantity > ZERO else Side.BUY
            factor = (
                BPS - self.close_slippage_bps
                if side is Side.SELL
                else BPS + self.close_slippage_bps
            ) / BPS
            rules = self.manager.codecs[venue].rules(symbol)
            price = _round_to_rules(ref * factor, rules, side)
            intent = OrderIntent(
                intent_id=new_id(),
                business_key=f"halt:{halt_id}:{venue.value}:{symbol}",
                account=account,
                venue=venue,
                symbol=symbol,
                side=side,
                order_type=OrderType.LIMIT,
                quantity=abs(position.quantity),
                created_at_ms=self.clock.now_ms(),
                trace_id=trace_id,
                limit_price=price,
                time_in_force=TimeInForce.IOC,
                reduce_only=True,
                strategy="emergency_stop",
            )
            view = self.manager.submit(intent)
            out.append(f"close:{intent.intent_id}:{view.status.value}")
        return out


def _round_to_rules(price: Decimal, rules: object, side: Side) -> Decimal:
    """Round a protective price onto the venue grid, in the direction that keeps it marketable."""
    tick = getattr(rules, "tick_size", None)
    if isinstance(tick, Decimal) and tick > ZERO:
        return quantize_down(price, tick) if side is Side.SELL else quantize_up(price, tick)
    return price.quantize(Decimal("0.01"))
