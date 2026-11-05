"""Audit log: risk decisions, signer refusals, mode changes, manual actions.

It reuses ``EventStore`` (append-only triggers, single writer) on its own file, so the signer
process and the trading process each keep an independent, tamper-evident trail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from humpback.core.clock import Clock
from humpback.ledger.store import EventStore, StoredEvent


class AuditLog:
    def __init__(self, path: str | Path, clock: Clock) -> None:
        self._store = EventStore(path)
        self._clock = clock

    def record(self, kind: str, trace_id: str, **detail: Any) -> None:
        self._store.append(kind, "audit", detail, at_ms=self._clock.now_ms(), trace_id=trace_id)

    def entries(self, kind: str | None = None) -> list[StoredEvent]:
        return [e for e in self._store.events() if kind is None or e.kind == kind]

    def close(self) -> None:
        self._store.close()
