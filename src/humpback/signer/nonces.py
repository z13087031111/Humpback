"""Per-signer nonce allocation.

Hyperliquid and Aster both keep only the most recent 100 nonces *per signer address* and
reject anything older, so nonces must be strictly increasing per signer and close to wall
time. One allocator owns each signer; the last value is persisted so a restart never reuses
or goes backwards.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from humpback.core.clock import Clock
from humpback.core.errors import SignerRefused

_SCHEMA = "CREATE TABLE IF NOT EXISTS nonces (signer TEXT PRIMARY KEY, last INTEGER NOT NULL)"


class NonceAllocator:
    def __init__(self, path: str | Path, clock: Clock, *, max_lead_ms: int = 30_000) -> None:
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(_SCHEMA)
        self._clock = clock
        self._max_lead_ms = max_lead_ms

    def next(self, signer: str, unit: Literal["ms", "us"]) -> int:
        signer = signer.lower()
        now = self._clock.now_ms() * (1000 if unit == "us" else 1)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute("SELECT last FROM nonces WHERE signer = ?", (signer,)).fetchone()
            last = -1 if row is None else int(row[0])
            nonce = max(now, last + 1)
            lead = nonce - now
            if lead > self._max_lead_ms * (1000 if unit == "us" else 1):
                raise SignerRefused(
                    f"nonce for {signer} would run {lead}{unit} ahead of time",
                    code="NONCE_AHEAD_OF_CLOCK",
                )
            self._db.execute(
                "INSERT INTO nonces(signer, last) VALUES (?, ?) "
                "ON CONFLICT(signer) DO UPDATE SET last = excluded.last",
                (signer, nonce),
            )
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        return nonce

    def close(self) -> None:
        self._db.close()
