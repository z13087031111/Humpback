"""Order manager: the only path from an intent to a venue.

    intent → capability + precision check → ledger (idempotent by business key)
           → risk gate → signer → SUBMIT_STARTED persisted → venue
           → ACCEPTED / REJECTED / UNKNOWN_IN_FLIGHT → fills ingested once

A submit is never retried automatically. If the outcome is unknown the order stays
``UNKNOWN_IN_FLIGHT`` and the unknown-order resolver queries the venue by client id.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from humpback.audit.log import AuditLog
from humpback.core.clock import Clock
from humpback.core.errors import (
    InvalidTransition,
    LedgerError,
    SignerRefused,
    SignerUnavailable,
    VenueOutcomeUnknown,
)
from humpback.core.ids import client_key
from humpback.domain.models import OrderIntent, PricingMode, Venue
from humpback.ledger.book import Ledger
from humpback.ledger.events import EventKind
from humpback.ledger.orders import OrderView
from humpback.risk.gate import Approval, MarketContext, Rejection, RiskGate
from humpback.risk.supervisor import UNKNOWN_ORDERS, Supervisor, TradingMode, recon_mismatch
from humpback.signer.service import SignerService
from humpback.venues.base import SubmitOutcome, VenueCodec, VenueTransport


class MarketData(Protocol):
    def context(self, venue: Venue, symbol: str) -> MarketContext: ...


@dataclass
class TransportMarketData:
    """Reference prices straight from the venue transports (enough for phase 1)."""

    transports: Mapping[Venue, VenueTransport]

    def context(self, venue: Venue, symbol: str) -> MarketContext:
        transport = self.transports.get(venue)
        price = None if transport is None else transport.reference_price(symbol)
        return MarketContext(price, PricingMode.NORMAL)

    def price(self, venue: Venue, symbol: str) -> Decimal | None:
        return self.context(venue, symbol).reference_price


class OrderManager:
    def __init__(
        self,
        *,
        ledger: Ledger,
        gate: RiskGate,
        signer: SignerService,
        codecs: Mapping[Venue, VenueCodec],
        transports: Mapping[Venue, VenueTransport],
        supervisor: Supervisor,
        market: MarketData,
        clock: Clock,
        audit: AuditLog,
    ) -> None:
        self.ledger = ledger
        self.gate = gate
        self.signer = signer
        self.codecs = dict(codecs)
        self.transports = dict(transports)
        self.supervisor = supervisor
        self.market = market
        self.clock = clock
        self.audit = audit

    # ------------------------------------------------------------------ submit

    def submit(self, intent: OrderIntent) -> OrderView:
        codec = self.codecs[intent.venue]
        codec.capabilities.ensure_supported(intent)
        market = self.market.context(intent.venue, intent.symbol)
        codec.rules(intent.symbol).check(intent, market.reference_price)

        view, created = self._record(intent, codec)
        if not created:
            return view  # same decision already recorded: never send it twice

        decision = self.gate.evaluate_place(view, market)
        if isinstance(decision, Rejection):
            self.audit.record(
                "risk_rejected",
                intent.trace_id,
                intent_id=intent.intent_id,
                reasons=list(decision.reasons),
            )
            return self._transition(
                view, EventKind.RISK_REJECTED, {"reason": ";".join(decision.reasons)}
            )
        self.audit.record("risk_approved", intent.trace_id, intent_id=intent.intent_id)
        return self._send_place(view, decision)

    def _record(self, intent: OrderIntent, codec: VenueCodec) -> tuple[OrderView, bool]:
        now = self.clock.now_ms()
        for salt in range(4):
            client_id = codec.encode_client_id(client_key(intent.business_key, salt))
            try:
                return self.ledger.record_intent(intent, client_id=client_id, at_ms=now)
            except LedgerError as exc:
                if "client id collision" not in str(exc):
                    raise
        raise LedgerError(f"could not allocate a client id for {intent.business_key}")

    def _send_place(self, view: OrderView, approval: Approval) -> OrderView:
        intent = view.intent
        request = self.codecs[intent.venue].build_place(intent, view.client_id)
        try:
            signed = self.signer.sign(request, approval, trace_id=intent.trace_id)
        except (SignerRefused, SignerUnavailable) as exc:
            return self._transition(view, EventKind.RISK_REJECTED, {"reason": f"signer:{exc.code}"})

        view = self._transition(view, EventKind.SUBMIT_STARTED, {"nonce": signed.nonce})
        transport = self.transports[intent.venue]
        try:
            result = transport.send(signed)
        except VenueOutcomeUnknown as exc:
            return self._unknown(view, str(exc))
        except Exception as exc:  # anything unexpected is treated as unknown, never as failure
            return self._unknown(view, f"{type(exc).__name__}: {exc}")

        if result.outcome is SubmitOutcome.REJECTED:
            view = self._transition(view, EventKind.VENUE_REJECTED, {"reason": result.reason})
        else:
            view = self._transition(
                view, EventKind.VENUE_ACCEPTED, {"venue_order_id": result.venue_order_id}
            )
            self.sync_fills(intent.venue, intent.account)
            view = self.ledger.order(intent.intent_id)
            if result.terminal_status in ("expired", "canceled"):
                kind = (
                    EventKind.ORDER_EXPIRED
                    if result.terminal_status == "expired"
                    else EventKind.CANCEL_CONFIRMED
                )
                view = self._transition(view, kind, {})
        return view

    # ------------------------------------------------------------------ cancel

    def cancel(self, intent_id: str) -> OrderView:
        view = self.ledger.order(intent_id)
        intent = view.intent
        decision = self.gate.approve_cancel(view)
        if isinstance(decision, Rejection):
            return view
        request = self.codecs[intent.venue].build_cancel(intent, view.client_id)
        signed = self.signer.sign(request, decision, trace_id=intent.trace_id)
        view = self._transition(view, EventKind.CANCEL_REQUESTED, {})
        try:
            result = self.transports[intent.venue].send(signed)
        except VenueOutcomeUnknown as exc:
            return self._unknown(view, f"cancel: {exc}")
        except Exception as exc:
            return self._unknown(view, f"cancel: {type(exc).__name__}: {exc}")
        self.sync_fills(intent.venue, intent.account)
        view = self.ledger.order(intent_id)
        if result.outcome is SubmitOutcome.ACCEPTED:
            return self._transition(view, EventKind.CANCEL_CONFIRMED, {})
        # Cancel refused (e.g. already filled): ask the venue what actually happened.
        return self.refresh_order(intent_id)

    # ------------------------------------------------------------------ status and fills

    def refresh_order(self, intent_id: str) -> OrderView:
        view = self.ledger.order(intent_id)
        intent = view.intent
        self.sync_fills(intent.venue, intent.account)
        result = self.transports[intent.venue].query_order(
            intent.account, intent.symbol, view.client_id
        )
        view = self.ledger.order(intent_id)
        try:
            return self._transition(
                view,
                EventKind.VENUE_STATUS_OBSERVED,
                {
                    "status": result.status,
                    "venue_order_id": result.venue_order_id,
                    "filled_qty": str(result.filled_qty),
                },
            )
        except InvalidTransition as exc:
            self._flag_mismatch(intent.venue, intent.account, str(exc))
            return view

    def sync_fills(self, venue: Venue, account: str) -> int:
        """Pull fill reports and record each venue fill exactly once."""
        recorded = 0
        for fill in self.transports[venue].poll_fills(account):
            try:
                self.ledger.order(fill.intent_id)
            except LedgerError:
                self._flag_mismatch(venue, account, f"fill {fill.venue_fill_id} for unknown order")
                continue
            try:
                if self.ledger.record_fill(fill, at_ms=self.clock.now_ms()):
                    recorded += 1
            except InvalidTransition as exc:
                self._flag_mismatch(venue, account, str(exc))
        return recorded

    # ------------------------------------------------------------------ helpers

    def _transition(
        self, view: OrderView, kind: EventKind, payload: dict[str, object]
    ) -> OrderView:
        return self.ledger.transition(
            view.intent.intent_id, kind, dict(payload), at_ms=self.clock.now_ms()
        )

    def _unknown(self, view: OrderView, cause: str) -> OrderView:
        view = self._transition(view, EventKind.OUTCOME_UNKNOWN, {"cause": cause})
        self.supervisor.raise_reason(
            UNKNOWN_ORDERS,
            TradingMode.REDUCE_ONLY,
            f"{view.intent.intent_id}: {cause}",
            self.clock.now_ms(),
        )
        self.audit.record(
            "outcome_unknown", view.intent.trace_id, intent_id=view.intent.intent_id, cause=cause
        )
        return view

    def _flag_mismatch(self, venue: Venue, account: str, detail: str) -> None:
        self.supervisor.raise_reason(
            recon_mismatch(venue.value, account),
            TradingMode.HALT_NEW_ORDERS,
            detail,
            self.clock.now_ms(),
        )
        self.audit.record(
            "ledger_mismatch", "system", venue=venue.value, account=account, detail=detail
        )
