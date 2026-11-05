"""Cryptographic test vectors from the official SDK / docs.

Hyperliquid vectors are copied from hyperliquid-python-sdk ``tests/signing_test.py``
(commit 2fdb18f). The Aster key/address pair is the demo pair printed in the official
Futures V3 docs. These need the ``signing`` extra and run in CI.
"""

from decimal import Decimal

import pytest

pytest.importorskip("eth_account")
pytest.importorskip("msgpack")

from eth_account import Account
from eth_account.messages import encode_typed_data

from humpback.domain.models import OrderIntent, OrderType, Side, TimeInForce, Venue
from humpback.signer.backends import (
    AsterAgentBackend,
    HyperliquidAgentBackend,
    aster_message,
    aster_typed_data,
    hyperliquid_action_hash,
)
from humpback.venues.hyperliquid import HyperliquidCodec, HyperliquidMarket
from humpback.venues.precision import HyperliquidRules

pytestmark = pytest.mark.signing

SDK_TEST_KEY = bytes.fromhex("0123456789012345678901234567890123456789012345678901234567890123")


def test_hyperliquid_action_hash_matches_production_example() -> None:
    action = {
        "type": "order",
        "orders": [
            {
                "a": 4,
                "b": True,
                "p": "1670.1",
                "s": "0.0147",
                "r": False,
                "t": {"limit": {"tif": "Ioc"}},
            }
        ],
        "grouping": "na",
    }
    digest = hyperliquid_action_hash(action, None, 1677777606040, None)
    assert "0x" + digest.hex() == (
        "0x0fcbeda5ae3c4950a548021552a4fea2226858c4453571bf3f24ba017eac2908"
    )


@pytest.mark.parametrize(
    ("mainnet", "r", "s", "v"),
    [
        (
            True,
            "0x53749d5b30552aeb2fca34b530185976545bb22d0b3ce6f62e31be961a59298",
            "0x755c40ba9bf05223521753995abb2f73ab3229be8ec921f350cb447e384d8ed8",
            27,
        ),
        (
            False,
            "0x542af61ef1f429707e3c76c5293c80d01f74ef853e34b76efffcb57e574f9510",
            "0x17b8b32f086e8cdede991f1e2c529f5dd5297cbe8128500e00cbaf766204a613",
            28,
        ),
    ],
)
def test_hyperliquid_l1_signature_dummy_action(mainnet: bool, r: str, s: str, v: int) -> None:
    backend = HyperliquidAgentBackend(SDK_TEST_KEY, is_mainnet=mainnet)
    signature = backend.sign_action({"type": "dummy", "num": 100_000_000_000}, 0)
    assert signature == {"r": r, "s": s, "v": v}


@pytest.mark.parametrize(
    ("mainnet", "r", "s", "v"),
    [
        (
            True,
            "0x41ae18e8239a56cacbc5dad94d45d0b747e5da11ad564077fcac71277a946e3",
            "0x3c61f667e747404fe7eea8f90ab0e76cc12ce60270438b2058324681a00116da",
            27,
        ),
        (
            False,
            "0xeba0664bed2676fc4e5a743bf89e5c7501aa6d870bdb9446e122c9466c5cd16d",
            "0x7f3e74825c9114bc59086f1eebea2928c190fdfbfde144827cb02b85bbe90988",
            28,
        ),
    ],
)
def test_hyperliquid_codec_order_signs_like_the_sdk(mainnet: bool, r: str, s: str, v: int) -> None:
    codec = HyperliquidCodec({"ETH": HyperliquidMarket("ETH", 1, HyperliquidRules(4))})
    intent = OrderIntent(
        intent_id="i",
        business_key="k",
        account="0xabc",
        venue=Venue.HYPERLIQUID,
        symbol="ETH",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal(100),
        created_at_ms=0,
        trace_id="t",
        limit_price=Decimal(100),
        time_in_force=TimeInForce.GTC,
    )
    request = codec.build_place(intent, "0x00000000000000000000000000000001")
    backend = HyperliquidAgentBackend(SDK_TEST_KEY, is_mainnet=mainnet)
    assert backend.sign_action(request.body["action"], 0) == {"r": r, "s": s, "v": v}


ASTER_DOC_KEY = bytes.fromhex("4fd0a42218f3eae43a6ce26d22544e986139a01e5b34a62db53757ffca81bae1")
ASTER_DOC_SIGNER = "0x21cF8Ae13Bb72632562c6Fff438652Ba1a151bb0"


def test_aster_signature_recovers_to_the_documented_signer() -> None:
    backend = AsterAgentBackend(ASTER_DOC_KEY, user="0x63DD5aCC6b1aa0f563956C0e534DD30B6dcF7C4e")
    assert backend.address == ASTER_DOC_SIGNER
    params = {
        "symbol": "ASTERUSDT",
        "type": "LIMIT",
        "side": "BUY",
        "timeInForce": "GTC",
        "quantity": "20",
        "price": "0.5",
    }
    message = aster_message(params, 1748310859508867, ASTER_DOC_SIGNER)
    assert message.endswith("&nonce=1748310859508867&signer=" + ASTER_DOC_SIGNER)
    signed = Account.from_key(ASTER_DOC_KEY).sign_message(
        encode_typed_data(full_message=aster_typed_data(message))
    )
    recovered = Account.recover_message(
        encode_typed_data(full_message=aster_typed_data(message)), signature=signed.signature
    )
    assert recovered == ASTER_DOC_SIGNER
