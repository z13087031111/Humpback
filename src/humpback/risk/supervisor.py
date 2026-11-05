"""Runtime supervisor: the trading mode is the most restrictive of all open reasons.

Anything unknown is unsafe. Unknown orders, a reconciliation mismatch, stale market data or
the startup lock each raise a reason; the mode only relaxes once *every* reason is cleared by
the component that raised it (with evidence), never by a timer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum


class TradingMode(IntEnum):
    """Ordered by restrictiveness: a higher value wins."""

    NORMAL = 0
    REDUCE_ONLY = 1
    HALT_NEW_ORDERS = 2
    HALTED = 3


@dataclass(frozen=True, slots=True)
class Reason:
    code: str
    mode: TradingMode
    detail: str
    raised_at_ms: int


Listener = Callable[[str, Reason | None, TradingMode], None]


class Supervisor:
    def __init__(self) -> None:
        self._reasons: dict[str, Reason] = {}
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    @property
    def mode(self) -> TradingMode:
        return max((r.mode for r in self._reasons.values()), default=TradingMode.NORMAL)

    def reasons(self) -> list[Reason]:
        return sorted(self._reasons.values(), key=lambda r: (-r.mode, r.code))

    def raise_reason(self, code: str, mode: TradingMode, detail: str, at_ms: int) -> None:
        existing = self._reasons.get(code)
        if existing is not None and existing.mode >= mode:
            return
        reason = Reason(code, mode, detail, at_ms)
        self._reasons[code] = reason
        for listener in self._listeners:
            listener(code, reason, self.mode)

    def clear_reason(self, code: str) -> None:
        if self._reasons.pop(code, None) is not None:
            for listener in self._listeners:
                listener(code, None, self.mode)

    def has(self, code: str) -> bool:
        return code in self._reasons

    def has_prefix(self, prefix: str) -> bool:
        return any(code == prefix or code.startswith(prefix + ":") for code in self._reasons)

    def allows_new_risk(self) -> bool:
        return self.mode is TradingMode.NORMAL

    def allows_reducing(self) -> bool:
        return self.mode <= TradingMode.HALT_NEW_ORDERS


# Standard reason codes
STARTUP_LOCK = "startup_lock"
UNKNOWN_ORDERS = "unknown_orders"
EMERGENCY_STOP = "emergency_stop"  # one reason per halt: "emergency_stop:<halt_id>"
DAILY_LOSS = "daily_loss_limit"


def emergency_stop(halt_id: str) -> str:
    return f"{EMERGENCY_STOP}:{halt_id}"


def recon_mismatch(venue: str, account: str) -> str:
    return f"recon_mismatch:{venue}:{account}"


def stale_market_data(venue: str) -> str:
    return f"stale_market_data:{venue}"
