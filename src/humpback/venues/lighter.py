"""Lighter codec (pure: no network).

Facts encoded here (verified 2026-11-05 against docs.lighter.xyz and lighter-python 38a3467):

* mainnet chain id 304, testnet 300; the Robinhood-chain deployment is a separate exchange;
* programmatic API keys use indices 4..254 (0..3 are reserved for the official apps);
* API keys can withdraw to the account's own L1 address, so the signer must refuse it;
* ``client_order_index`` is a uint48, unique across all markets; 0 is used by the front end;
* prices and sizes are integers scaled by ``price_decimals`` / ``size_decimals``;
* order types: limit 0, market 1, stop-loss 2, stop-loss-limit 3, take-profit 4,
  take-profit-limit 5, TWAP 6; TIF: IOC 0, good-till-time 1, post-only 2;
* ``sendTx`` returning 200 means *accepted for processing*, not executed.

Market and market-trigger orders also need a worst-price bound on Lighter; until that is
modelled explicitly they are declared unsupported.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from humpback.core.errors import UnsupportedCapability
from humpback.domain.models import OrderIntent, OrderType, Side, TimeInForce, Venue
from humpback.venues.base import Action, UnsignedRequest, VenueCapabilities
from humpback.venues.precision import LighterRules

MAINNET_CHAIN_ID = 304
TESTNET_CHAIN_ID = 300
PROGRAMMATIC_KEY_INDICES = range(4, 255)
DEFAULT_ORDER_EXPIRY = -1  # SDK constant: 28-day default expiry
IOC_EXPIRY = 0

CAPABILITIES = VenueCapabilities(
    venue=Venue.LIGHTER,
    order_types=frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT_LIMIT}),
    time_in_force=frozenset({TimeInForce.GTC, TimeInForce.IOC, TimeInForce.POST_ONLY}),
    reduce_only=True,
    query_by_client_id=False,
    cancel_by_client_id=True,
    client_id_format="uint48 client_order_index, never 0",
    signature_scheme="lighter-go signer (official binary)",
    nonce_unit="ms",
    margin_modes=frozenset({"cross", "isolated"}),
    notes=("no direct lookup by client_order_index; resolve via active/inactive order lists",),
)

_TYPE = {OrderType.LIMIT: 0, OrderType.STOP_LIMIT: 3, OrderType.TAKE_PROFIT_LIMIT: 5}
_TIF = {TimeInForce.IOC: 0, TimeInForce.GTC: 1, TimeInForce.POST_ONLY: 2}


def check_api_key_index(index: int) -> int:
    if index not in PROGRAMMATIC_KEY_INDICES:
        raise ValueError(f"lighter API key index {index} is reserved; use 4..254")
    return index


@dataclass(frozen=True, slots=True)
class LighterMarket:
    symbol: str
    market_index: int
    rules: LighterRules


@dataclass
class LighterCodec:
    markets: dict[str, LighterMarket] = field(default_factory=dict)

    @property
    def capabilities(self) -> VenueCapabilities:
        return CAPABILITIES

    def market(self, symbol: str) -> LighterMarket:
        try:
            return self.markets[symbol]
        except KeyError:
            raise UnsupportedCapability(f"lighter market {symbol} not configured") from None

    def rules(self, symbol: str) -> LighterRules:
        return self.market(symbol).rules

    def encode_client_id(self, key: bytes) -> str:
        index = int.from_bytes(key[:6], "big")
        if index == 0:
            raise ValueError("client_order_index 0 is reserved; retry with another salt")
        return str(index)

    def build_place(self, intent: OrderIntent, client_id: str) -> UnsignedRequest:
        CAPABILITIES.ensure_supported(intent)
        market = self.market(intent.symbol)
        assert intent.limit_price is not None
        body: dict[str, Any] = {
            "tx": "create_order",
            "market_index": market.market_index,
            "client_order_index": int(client_id),
            "base_amount": market.rules.to_base_int(intent.quantity),
            "price": market.rules.to_price_int(intent.limit_price),
            "is_ask": intent.side is Side.SELL,
            "order_type": _TYPE[intent.order_type],
            "time_in_force": _TIF[intent.time_in_force],
            "reduce_only": intent.reduce_only,
            "trigger_price": (
                0
                if intent.trigger_price is None
                else market.rules.to_price_int(intent.trigger_price)
            ),
            "order_expiry": (
                IOC_EXPIRY if intent.time_in_force is TimeInForce.IOC else DEFAULT_ORDER_EXPIRY
            ),
        }
        return UnsignedRequest(
            Venue.LIGHTER,
            Action.PLACE_ORDER,
            intent.account,
            intent.intent_id,
            client_id,
            intent.content_hash(),
            body,
        )

    def build_cancel(self, intent: OrderIntent, client_id: str) -> UnsignedRequest:
        market = self.market(intent.symbol)
        body: dict[str, Any] = {
            "tx": "cancel_order",
            "market_index": market.market_index,
            "order_index": int(client_id),
        }
        return UnsignedRequest(
            Venue.LIGHTER,
            Action.CANCEL_ORDER,
            intent.account,
            intent.intent_id,
            client_id,
            intent.content_hash(),
            body,
        )
