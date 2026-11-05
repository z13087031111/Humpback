"""Signing backends, one per venue.

Real backends need the optional ``signing`` extra (``eth-account``, ``msgpack``). Their
algorithms are pinned by test vectors taken from the official SDKs/docs
(``tests/test_signing_vectors.py``), which run in CI where those packages are installed.

Keys are loaded from an encrypted keystore and kept only in this process's memory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from humpback.core.errors import SignerUnavailable
from humpback.domain.models import Venue
from humpback.venues.base import UnsignedRequest
from humpback.venues.lighter import check_api_key_index


class SigningBackend(Protocol):
    venue: Venue

    @property
    def address(self) -> str: ...

    def sign(self, request: UnsignedRequest, nonce: int) -> tuple[str, dict[str, Any]]:
        """Return the signature (venue format) and any extra fields the transport needs."""


def _eth() -> Any:
    try:
        import eth_account
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SignerUnavailable("install the 'signing' extra (eth-account)") from exc
    return eth_account


def load_private_key(keystore_path: str | Path, passphrase: str) -> bytes:
    """Decrypt a standard Ethereum keystore (V3 JSON). The passphrase is never stored."""
    eth_account = _eth()
    data = Path(keystore_path).read_text()
    return bytes(eth_account.Account.decrypt(data, passphrase))


class TestBackend:
    """Deterministic HMAC "signatures" for local tests. Never accepted by a real venue."""

    __test__ = False  # not a pytest test class

    def __init__(self, venue: Venue, address: str, secret: bytes = b"humpback-test") -> None:
        self.venue = venue
        self._address = address.lower()
        self._secret = secret

    @property
    def address(self) -> str:
        return self._address

    def sign(self, request: UnsignedRequest, nonce: int) -> tuple[str, dict[str, Any]]:
        message = f"{request.intent_id}|{request.client_id}|{request.action}|{nonce}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest(), {}


# ---------------------------------------------------------------------- Hyperliquid


def hyperliquid_action_hash(
    action: Any, vault_address: str | None, nonce: int, expires_after: int | None
) -> bytes:
    """keccak(msgpack(action) ‖ nonce(8B) ‖ vault flag[‖ vault] [‖ 0x00 ‖ expiresAfter(8B)])."""
    try:
        import msgpack
        from eth_utils.crypto import keccak
    except ImportError as exc:  # pragma: no cover
        raise SignerUnavailable("install the 'signing' extra (msgpack, eth-account)") from exc
    data: bytes = msgpack.packb(action)
    data += nonce.to_bytes(8, "big")
    if vault_address is None:
        data += b"\x00"
    else:
        data += b"\x01" + bytes.fromhex(vault_address.lower().removeprefix("0x"))
    if expires_after is not None:
        data += b"\x00" + expires_after.to_bytes(8, "big")
    return bytes(keccak(data))


def hyperliquid_l1_typed_data(connection_id: bytes, is_mainnet: bool) -> dict[str, Any]:
    return {
        "domain": {
            "chainId": 1337,
            "name": "Exchange",
            "verifyingContract": "0x0000000000000000000000000000000000000000",
            "version": "1",
        },
        "types": {
            "Agent": [
                {"name": "source", "type": "string"},
                {"name": "connectionId", "type": "bytes32"},
            ],
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": "Agent",
        "message": {"source": "a" if is_mainnet else "b", "connectionId": connection_id},
    }


@dataclass
class HyperliquidAgentBackend:
    """Signs L1 actions (orders, cancels) with an agent (API) wallet key."""

    private_key: bytes
    is_mainnet: bool
    vault_address: str | None = None
    venue: Venue = Venue.HYPERLIQUID

    @property
    def address(self) -> str:
        return str(_eth().Account.from_key(self.private_key).address).lower()

    def sign_action(
        self, action: Any, nonce: int, expires_after: int | None = None
    ) -> dict[str, Any]:
        eth_account = _eth()
        from eth_account.messages import encode_typed_data
        from eth_utils.conversions import to_hex

        connection_id = hyperliquid_action_hash(action, self.vault_address, nonce, expires_after)
        typed = hyperliquid_l1_typed_data(connection_id, self.is_mainnet)
        signed = eth_account.Account.from_key(self.private_key).sign_message(
            encode_typed_data(full_message=typed)
        )
        return {"r": to_hex(signed.r), "s": to_hex(signed.s), "v": signed.v}

    def sign(self, request: UnsignedRequest, nonce: int) -> tuple[str, dict[str, Any]]:
        signature = self.sign_action(request.body["action"], nonce)
        return json.dumps(signature, separators=(",", ":")), {"signature": signature}


# ---------------------------------------------------------------------- Aster V3

ASTER_DOMAIN = {
    "name": "AsterSignTransaction",
    "version": "1",
    "chainId": 1666,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}


def aster_message(params: dict[str, str], nonce: int, signer: str, user: str | None = None) -> str:
    """URL-encoded request parameters, in the order the official example builds them."""
    body = dict(params)
    body["nonce"] = str(nonce)
    if user is not None:
        body["user"] = user
    body["signer"] = signer
    return urllib.parse.urlencode(body)


def aster_typed_data(message: str) -> dict[str, Any]:
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Message": [{"name": "msg", "type": "string"}],
        },
        "primaryType": "Message",
        "domain": ASTER_DOMAIN,
        "message": {"msg": message},
    }


@dataclass
class AsterAgentBackend:
    """Signs V3 trade requests with an agent created with ``canWithdraw=false``.

    Whether ``user`` must be part of the signed message in agent mode is not settled by the
    docs (the official example omits it); ``include_user`` stays False until verified on
    testnet.
    """

    private_key: bytes
    user: str
    include_user: bool = False
    venue: Venue = Venue.ASTER

    @property
    def address(self) -> str:
        return str(_eth().Account.from_key(self.private_key).address)

    def sign(self, request: UnsignedRequest, nonce: int) -> tuple[str, dict[str, Any]]:
        eth_account = _eth()
        from eth_account.messages import encode_typed_data

        message = aster_message(
            request.body["params"], nonce, self.address, self.user if self.include_user else None
        )
        signed = eth_account.Account.from_key(self.private_key).sign_message(
            encode_typed_data(full_message=aster_typed_data(message))
        )
        signature = "0x" + bytes(signed.signature).hex()
        return signature, {"message": message, "signer": self.address}


class LighterBackend:
    """Lighter transactions are signed by the official lighter-go signer binary.

    Wiring it in is part of milestone 1.3 (testnet); until then this backend refuses.
    """

    venue = Venue.LIGHTER

    def __init__(self, api_key_index: int) -> None:
        self.api_key_index = check_api_key_index(api_key_index)

    @property
    def address(self) -> str:
        return f"lighter-key-{self.api_key_index}"

    def sign(self, request: UnsignedRequest, nonce: int) -> tuple[str, dict[str, Any]]:
        raise SignerUnavailable("lighter signer binary not wired yet (milestone 1.3)")
