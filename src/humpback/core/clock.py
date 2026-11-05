"""Time sources. All timestamps are integer milliseconds since the Unix epoch, UTC."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000


class ManualClock:
    """Deterministic clock for tests and replays."""

    def __init__(self, start_ms: int = 1_767_225_600_000) -> None:  # 2026-01-01T00:00:00Z
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def advance(self, ms: int) -> None:
        if ms < 0:
            raise ValueError("time cannot go backwards")
        self._now += ms

    def set(self, now_ms: int) -> None:
        if now_ms < self._now:
            raise ValueError("time cannot go backwards")
        self._now = now_ms


DAY_MS = 86_400_000


def utc_day(ts_ms: int) -> int:
    """Index of the UTC calendar day containing ``ts_ms``."""
    return ts_ms // DAY_MS
