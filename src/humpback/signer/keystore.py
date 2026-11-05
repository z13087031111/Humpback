"""Encrypted key files for trading (agent / API) keys.

One format for all three venues: Hyperliquid and Aster agent keys are 32-byte secp256k1
keys, Lighter API keys use their own scheme and length, so the standard Ethereum V3 keystore
(which derives an address) does not fit Lighter.

Format (JSON):
    scrypt(passphrase, salt) -> 32-byte key -> AES-256-GCM(private key bytes)
    the venue, label and public address are stored in clear text and are bound to the
    ciphertext as associated data, so they cannot be swapped without detection.

Rules:
* the private key exists in clear only in memory of the process that loads it;
* files are written with mode 0600 inside a 0700 directory and are never overwritten;
* only agent / API keys belong here, never a main wallet key (that stays on a hardware wallet).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from humpback.core.errors import SignerUnavailable
from humpback.domain.models import Venue

FORMAT = "humpback-keystore"
VERSION = 1
MIN_PASSPHRASE = 12
# scrypt cost: 2**17 * 8 * 128 bytes = 128 MiB of memory per guess.
DEFAULT_SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1

_ETH_VENUES = frozenset({Venue.HYPERLIQUID, Venue.ASTER})


class KeystoreError(ValueError):
    """Bad key, bad passphrase, tampered file, or unsafe location."""


@dataclass(frozen=True, slots=True)
class KeyInfo:
    venue: Venue
    label: str
    address: str | None
    fingerprint: str  # first 8 bytes of sha256(key), safe to show and compare


def _aes() -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SignerUnavailable("install the 'signing' extra (cryptography)") from exc
    return AESGCM


def parse_private_key(text: str, venue: Venue) -> bytes:
    """Accept hex with or without ``0x``. Ethereum-style venues need exactly 32 bytes."""
    cleaned = text.strip().removeprefix("0x").removeprefix("0X")
    if not cleaned or len(cleaned) % 2:
        raise KeystoreError("private key must be an even number of hex characters")
    try:
        key = bytes.fromhex(cleaned)
    except ValueError:
        raise KeystoreError("private key must be hexadecimal") from None
    if venue in _ETH_VENUES and len(key) != 32:
        raise KeystoreError(f"{venue} agent keys are 32 bytes (64 hex characters)")
    if not 16 <= len(key) <= 128:
        raise KeystoreError("private key length is implausible")
    if not any(key):
        raise KeystoreError("private key is all zeros")
    return key


def eth_address(key: bytes) -> str | None:
    """Checksummed address for secp256k1 keys, if eth-account is installed."""
    try:
        from eth_account import Account
    except ImportError:
        return None
    return str(Account.from_key(key).address)


def fingerprint(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _derive(passphrase: str, salt: bytes, n: int) -> bytes:
    return hashlib.scrypt(
        passphrase.encode(),
        salt=salt,
        n=n,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
        maxmem=n * SCRYPT_R * 128 * 2,
    )


def _associated(venue: str, label: str, address: str | None) -> bytes:
    return json.dumps([FORMAT, VERSION, venue, label, address], separators=(",", ":")).encode()


def check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise KeystoreError(f"passphrase must be at least {MIN_PASSPHRASE} characters")


def create(
    path: str | Path,
    key: bytes,
    passphrase: str,
    *,
    venue: Venue,
    label: str,
    scrypt_n: int = DEFAULT_SCRYPT_N,
) -> KeyInfo:
    check_passphrase(passphrase)
    target = Path(path)
    if target.exists():
        raise KeystoreError(f"{target} already exists; keystores are never overwritten")
    address = eth_address(key) if venue in _ETH_VENUES else None
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    aad = _associated(venue.value, label, address)
    ciphertext = _aes()(_derive(passphrase, salt, scrypt_n)).encrypt(nonce, key, aad)
    document = {
        "format": FORMAT,
        "version": VERSION,
        "venue": venue.value,
        "label": label,
        "address": address,
        "fingerprint": fingerprint(key),
        "kdf": {
            "name": "scrypt",
            "n": scrypt_n,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt": base64.b64encode(salt).decode(),
        },
        "cipher": {
            "name": "aes-256-gcm",
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
        },
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    return KeyInfo(venue, label, address, fingerprint(key))


def read_info(path: str | Path) -> KeyInfo:
    """Public metadata only; no passphrase needed."""
    doc = _read(path)
    return KeyInfo(Venue(doc["venue"]), doc["label"], doc["address"], doc["fingerprint"])


def load(path: str | Path, passphrase: str) -> tuple[bytes, KeyInfo]:
    """Decrypt. Raises KeystoreError on a wrong passphrase or any tampering."""
    target = Path(path)
    mode = target.stat().st_mode & 0o777
    if mode & 0o077:
        raise KeystoreError(f"{target} is readable by other users (mode {mode:o}); chmod 600")
    doc = _read(target)
    kdf, cipher = doc["kdf"], doc["cipher"]
    if kdf.get("name") != "scrypt" or cipher.get("name") != "aes-256-gcm":
        raise KeystoreError("unsupported keystore algorithms")
    derived = _derive(passphrase, base64.b64decode(kdf["salt"]), int(kdf["n"]))
    aad = _associated(doc["venue"], doc["label"], doc["address"])
    try:
        key = _aes()(derived).decrypt(
            base64.b64decode(cipher["nonce"]), base64.b64decode(cipher["ciphertext"]), aad
        )
    except Exception:
        raise KeystoreError("wrong passphrase or tampered keystore") from None
    info = KeyInfo(Venue(doc["venue"]), doc["label"], doc["address"], fingerprint(key))
    if info.fingerprint != doc["fingerprint"]:  # pragma: no cover - GCM already authenticates
        raise KeystoreError("fingerprint mismatch")
    return key, info


def _read(path: str | Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(Path(path).read_text())
    if doc.get("format") != FORMAT or doc.get("version") != VERSION:
        raise KeystoreError("not a Humpback keystore")
    return doc
