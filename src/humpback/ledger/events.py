"""Event kinds. Events are facts; projections (orders, positions, balances) are derived."""

from __future__ import annotations

from enum import StrEnum


class EventKind(StrEnum):
    # Order lifecycle (stream "order:<intent_id>")
    INTENT_RECORDED = "intent_recorded"
    RISK_REJECTED = "risk_rejected"
    SUBMIT_STARTED = "submit_started"
    VENUE_ACCEPTED = "venue_accepted"
    VENUE_REJECTED = "venue_rejected"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_CONFIRMED = "cancel_confirmed"
    ORDER_EXPIRED = "order_expired"
    FILL_RECORDED = "fill_recorded"
    VENUE_STATUS_OBSERVED = "venue_status_observed"
    NOT_FOUND_CONFIRMED = "not_found_confirmed"

    # Account (stream "account:<venue>:<account>")
    COLLATERAL_ADJUSTED = "collateral_adjusted"
    FUNDING_RECORDED = "funding_recorded"

    # Capital transfers (stream "transfer:<transfer_id>")
    TRANSFER_EVENT = "transfer_event"

    # Emergency stop (stream "halt:<halt_id>")
    HALT_STAGE = "halt_stage"


def order_stream(intent_id: str) -> str:
    return f"order:{intent_id}"


def account_stream(venue: str, account: str) -> str:
    return f"account:{venue}:{account}"
