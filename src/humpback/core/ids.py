"""Identifier helpers.

A *business key* names one trading decision (for example ``"carry:BTC:2026-11-05T08"``).
The client order key is derived from it deterministically, so a retry of the same decision
always produces the same client order id and the venue can reject the duplicate. Each venue
then encodes the 16-byte key in its own format (see ``humpback.venues``).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex


def new_trace_id() -> str:
    return "tr_" + uuid.uuid4().hex


def client_key(business_key: str, salt: int = 0) -> bytes:
    """16 bytes derived from the business key. ``salt`` only changes on a detected collision."""
    if not business_key or len(business_key) > 200:
        raise ValueError("business key must be 1..200 characters")
    material = f"{business_key}\x00{salt}".encode()
    return hashlib.sha256(material).digest()[:16]


def canonical_json(value: Any) -> str:
    """Stable JSON for hashing: sorted keys, no whitespace, no NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()
