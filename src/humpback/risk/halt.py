"""Emergency stop with three separately recorded stages.

    HALT_DECIDED               the decision to stop (who, why, scope)
    CANCEL_OR_CLOSE_REQUESTED  every cancel / reduce-only close attempted, and any failures
    VENUE_EXECUTION_CONFIRMED  the venues confirm it: no live orders, positions flat if a
                               close was requested, and reconciliation clean

Sending a close is not closing a position. ``confirm`` refuses to record the last stage until
the ledger *and* the venue agree.

Each halt raises its own supervisor reason (``emergency_stop:<halt_id>``), so resuming one
halt never lifts another. Halts are persisted in the ledger; on restart every halt that was
not resumed is raised again before anything can trade.

One failing cancel or close never stops the rest: each is attempted independently and its
failure is recorded in stage 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from humpback.core.ids import new_id, new_trace_id
from humpback.core.money import BPS, ZERO
from humpback.domain.models import OrderIntent, OrderType, Side, TimeInForce, Venue
from humpback.engine import OrderManager
from humpback.ledger.events import EventKind
from humpback.recon.reconciler import Reconciler
from humpback.risk.supervisor import TradingMode, emergency_stop
from humpback.venues.base import InstrumentRules


class HaltStage(StrEnum):
    HALT_DECIDED = "halt_decided"
    CANCEL_OR_CLOSE_REQUESTED = "cancel_or_close_requested"
    VENUE_EXECUTION_CONFIRMED = "venue_execution_confirmed"


@dataclass(frozen=True, slots=True)
class HaltStatus:
    halt_id: str
    stage: HaltStage
    pending: tuple[str, ...]


Scope = tuple[list[tuple[Venue, str]], bool]


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
        self._scope: dict[str, Scope] = {}
        self._restore()

    # ------------------------------------------------------------------ public

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
        self.supervisor.raise_reason(
            emergency_stop(halt_id), TradingMode.HALT_NEW_ORDERS, reason, self.clock.now_ms()
        )
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
                if view.intent.account != account:
                    continue
                order_id = view.intent.intent_id
                try:
                    after = self.manager.cancel(order_id)
                    requested.append(f"cancel:{order_id}:{after.status.value}")
                except Exception as exc:
                    requested.append(f"cancel_failed:{order_id}:{type(exc).__name__}")
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
        if self.stage(halt_id) is not HaltStage.VENUE_EXECUTION_CONFIRMED:
            self._stage(halt_id, HaltStage.VENUE_EXECUTION_CONFIRMED, new_trace_id())
        return HaltStatus(halt_id, HaltStage.VENUE_EXECUTION_CONFIRMED, ())

    def resume(self, halt_id: str, operator: str) -> None:
        if self.stage(halt_id) is not HaltStage.VENUE_EXECUTION_CONFIRMED:
            raise RuntimeError("cannot resume before the venues confirm the halt")
        self._stage(
            halt_id, HaltStage.VENUE_EXECUTION_CONFIRMED, new_trace_id(), resumed_by=operator
        )
        self.supervisor.clear_reason(emergency_stop(halt_id))

    def stage(self, halt_id: str) -> HaltStage:
        stages = [HaltStage(e.payload["stage"]) for e in self.ledger.events_for(f"halt:{halt_id}")]
        if not stages:
            raise KeyError(halt_id)
        return stages[-1]

    def open_halts(self) -> list[str]:
        return list(self._scope)

    # ------------------------------------------------------------------ internals

    def _restore(self) -> None:
        """Re-raise every halt that was never resumed (survives a restart)."""
        decided: dict[str, Scope] = {}
        resumed: set[str] = set()
        for event in self.ledger.store.events():
            if event.kind != EventKind.HALT_STAGE:
                continue
            halt_id = event.stream.removeprefix("halt:")
            payload = event.payload
            if payload["stage"] == HaltStage.HALT_DECIDED:
                accounts = []
                for item in payload["accounts"]:
                    venue, _, account = item.partition(":")
                    accounts.append((Venue(venue), account))
                decided[halt_id] = (accounts, bool(payload["close_positions"]))
            if payload.get("resumed_by"):
                resumed.add(halt_id)
        for halt_id, scope in decided.items():
            if halt_id in resumed:
                continue
            self._scope[halt_id] = scope
            self.supervisor.raise_reason(
                emergency_stop(halt_id),
                TradingMode.HALT_NEW_ORDERS,
                "restored after restart",
                self.clock.now_ms(),
            )

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
            try:
                out.append(
                    self._close_one(
                        halt_id=halt_id,
                        venue=venue,
                        account=account,
                        symbol=symbol,
                        quantity=position.quantity,
                        trace_id=trace_id,
                    )
                )
            except Exception as exc:
                out.append(f"close_failed:{symbol}:{type(exc).__name__}")
        return out

    def _close_one(
        self,
        *,
        halt_id: str,
        venue: Venue,
        account: str,
        symbol: str,
        quantity: Decimal,
        trace_id: str,
    ) -> str:
        ref = self.manager.market.context(venue, symbol).reference_price
        if ref is None:
            return f"close_skipped_no_price:{symbol}"
        side = Side.SELL if quantity > ZERO else Side.BUY
        slippage = self.close_slippage_bps if side is Side.BUY else -self.close_slippage_bps
        rules = self.manager.codecs[venue].rules(symbol)
        price = _round_to_rules(ref * (BPS + slippage) / BPS, rules, side)
        intent = OrderIntent(
            intent_id=new_id(),
            business_key=f"halt:{halt_id}:{venue.value}:{account}:{symbol}",
            account=account,
            venue=venue,
            symbol=symbol,
            side=side,
            order_type=OrderType.LIMIT,
            quantity=abs(quantity),
            created_at_ms=self.clock.now_ms(),
            trace_id=trace_id,
            limit_price=price,
            time_in_force=TimeInForce.IOC,
            reduce_only=True,
            strategy="emergency_stop",
        )
        view = self.manager.submit(intent)
        return f"close:{intent.intent_id}:{view.status.value}"


def _round_to_rules(price: Decimal, rules: InstrumentRules, side: Side) -> Decimal:
    """Round a protective price onto the venue grid, in the direction that keeps it marketable."""
    return rules.round_price(price, side)
