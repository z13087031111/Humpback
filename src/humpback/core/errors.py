"""Error types. Every error carries a stable machine-readable ``code``."""

from __future__ import annotations


class HumpbackError(Exception):
    code = "HUMPBACK_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message or self.code)
        if code is not None:
            self.code = code


class UnsupportedCapability(HumpbackError):
    """The venue does not declare support for what the caller asked for."""

    code = "UNSUPPORTED_CAPABILITY"


class PrecisionError(HumpbackError):
    code = "PRECISION_VIOLATION"


class LedgerError(HumpbackError):
    code = "LEDGER_ERROR"


class InvalidTransition(LedgerError):
    code = "INVALID_TRANSITION"


class SignerRefused(HumpbackError):
    code = "SIGNER_REFUSED"


class SignerUnavailable(HumpbackError):
    code = "SIGNER_UNAVAILABLE"


class VenueError(HumpbackError):
    """The venue answered and the answer is a definitive failure."""

    code = "VENUE_ERROR"


class VenueOutcomeUnknown(HumpbackError):
    """The request may or may not have reached the venue (timeout, reset, 5xx).

    Callers must never treat this as a failure: the order may be live.
    """

    code = "VENUE_OUTCOME_UNKNOWN"
