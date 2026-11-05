"""SQLite append-only event store.

Guarantees enforced by the database itself, not by convention:

* rows in ``events`` and ``dedupe`` can never be updated or deleted (triggers abort);
* a dedupe key (for example a venue fill id) can be appended at most once;
* only one process can open a given file for writing (an exclusive ``flock``).
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from humpback.core.errors import LedgerError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT    NOT NULL,
    stream   TEXT    NOT NULL,
    at_ms    INTEGER NOT NULL,
    trace_id TEXT    NOT NULL,
    payload  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS events_stream ON events(stream, seq);
CREATE TABLE IF NOT EXISTS dedupe (
    key TEXT PRIMARY KEY,
    seq INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS dedupe_no_update BEFORE UPDATE ON dedupe
BEGIN SELECT RAISE(ABORT, 'dedupe is append-only'); END;
CREATE TRIGGER IF NOT EXISTS dedupe_no_delete BEFORE DELETE ON dedupe
BEGIN SELECT RAISE(ABORT, 'dedupe is append-only'); END;
"""


@dataclass(frozen=True, slots=True)
class StoredEvent:
    seq: int
    kind: str
    stream: str
    at_ms: int
    trace_id: str
    payload: dict[str, Any]


class EventStore:
    """Single-writer store. Use ``":memory:"`` for tests."""

    def __init__(self, path: str | Path) -> None:
        self._lock_file: IO[str] | None = None
        if str(path) != ":memory:":
            db_path = Path(path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            lock = db_path.with_suffix(db_path.suffix + ".lock").open("a+")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise LedgerError(f"ledger already open by another process: {db_path}") from None
            self._lock_file = lock
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._depth = 0

    def close(self) -> None:
        self._db.close()
        if self._lock_file is not None:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group several appends atomically. Nested calls join the outer transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")
        finally:
            self._depth = 0

    def append(
        self,
        kind: str,
        stream: str,
        payload: dict[str, Any],
        *,
        at_ms: int,
        trace_id: str,
        dedupe_key: str | None = None,
    ) -> StoredEvent | None:
        """Append one event. Returns ``None`` if ``dedupe_key`` was already used."""
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.transaction():
            if dedupe_key is not None:
                existing = self._db.execute(
                    "SELECT seq FROM dedupe WHERE key = ?", (dedupe_key,)
                ).fetchone()
                if existing is not None:
                    return None
            cursor = self._db.execute(
                "INSERT INTO events(kind, stream, at_ms, trace_id, payload) VALUES (?,?,?,?,?)",
                (kind, stream, at_ms, trace_id, body),
            )
            seq = cursor.lastrowid
            if seq is None:  # pragma: no cover - sqlite always returns a rowid here
                raise LedgerError("insert returned no rowid")
            if dedupe_key is not None:
                self._db.execute("INSERT INTO dedupe(key, seq) VALUES (?, ?)", (dedupe_key, seq))
        return StoredEvent(seq, kind, stream, at_ms, trace_id, json.loads(body))

    def dedupe_seq(self, key: str) -> int | None:
        row = self._db.execute("SELECT seq FROM dedupe WHERE key = ?", (key,)).fetchone()
        return None if row is None else int(row[0])

    def get(self, seq: int) -> StoredEvent:
        row = self._db.execute(
            "SELECT seq, kind, stream, at_ms, trace_id, payload FROM events WHERE seq = ?", (seq,)
        ).fetchone()
        if row is None:
            raise LedgerError(f"no event {seq}")
        return _row(row)

    def events(self, after_seq: int = 0) -> Iterator[StoredEvent]:
        cursor = self._db.execute(
            "SELECT seq, kind, stream, at_ms, trace_id, payload FROM events "
            "WHERE seq > ? ORDER BY seq",
            (after_seq,),
        )
        for row in cursor:
            yield _row(row)

    def raw_connection_for_tests(self) -> sqlite3.Connection:
        return self._db


def _row(row: tuple[Any, ...]) -> StoredEvent:
    return StoredEvent(
        seq=int(row[0]),
        kind=str(row[1]),
        stream=str(row[2]),
        at_ms=int(row[3]),
        trace_id=str(row[4]),
        payload=json.loads(row[5]),
    )
