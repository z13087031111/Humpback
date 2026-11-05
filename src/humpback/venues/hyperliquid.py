"""Hyperliquid codec (pure: no network).

Facts encoded here (verified 2026-11-05 against the official docs and Python SDK):

* order wire key order is ``a, b, p, s, r, t, c``; msgpack hashing depends on it;
* numbers are strings without trailing zeros;
* ``cloid`` is 16 bytes as ``0x`` + 32 hex chars; ``orderStatus`` accepts it as ``oid``;
* limit TIF values are ``Gtc``, ``Ioc``, ``Alo`` (post-only);
* trigger orders carry ``triggerPx``, ``isMarket`` and ``tpsl``;
* there is no native market order: a "market" order is an IOC limit with a price cap, so this
  codec declares no MARKET support and callers must send LIMIT + IOC with an explicit price;
* L1 actions (orders, cancels) are signed per signer address with millisecond nonces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from humpback.core.errors import UnsupportedCapability
from humpback.core.money import to_wire
from humpback.domain.models import OrderIntent, OrderType, Side, TimeInForce, Venue
from humpback.venues.base import Action, UnsignedRequest, VenueCapabilities
from humpback.venues.precision import HyperliquidRules

CAPABILITIES = VenueCapabilities(
    venue=Venue.HYPERLIQUID,
    order_types=frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TAKE_PROFIT_LIMIT}),
    time_in_force=frozenset({TimeInForce.GTC, TimeInForce.IOC, TimeInForce.POST_ONLY}),
    reduce_only=True,
    query_by_client_id=True,
    cancel_by_client_id=True,
    client_id_format="0x + 32 hex (16 bytes)",
    signature_scheme="eip712 L1 action, domain Exchange chainId 1337",
    nonce_unit="ms",
    margin_modes=frozenset({"cross", "isolated"}),
    notes=(
        "no native market order: use LIMIT + IOC with a price cap",
        "market-trigger orders need a slippage cap price; not modelled yet",
    ),
)

_TIF = {TimeInForce.GTC: "Gtc", TimeInForce.IOC: "Ioc", TimeInForce.POST_ONLY: "Alo"}


@dataclass(frozen=True, slots=True)
class HyperliquidMarket:
    symbol: str
    asset: int  # perps: meta index; spot: 10000 + index; HIP-3: 100000 + dex*10000 + index
    rules: HyperliquidRules


@dataclass
class HyperliquidCodec:
    markets: dict[str, HyperliquidMarket] = field(default_factory=dict)

    @property
    def capabilities(self) -> VenueCapabilities:
        return CAPABILITIES

    def market(self, symbol: str) -> HyperliquidMarket:
        try:
            return self.markets[symbol]
        except KeyError:
            raise UnsupportedCapability(f"hyperliquid market {symbol} not configured") from None

    def rules(self, symbol: str) -> HyperliquidRules:
        return self.market(symbol).rules

    def encode_client_id(self, key: bytes) -> str:
        if len(key) != 16:
            raise ValueError("hyperliquid cloid needs 16 bytes")
        return "0x" + key.hex()

    def order_wire(self, intent: OrderIntent, client_id: str) -> dict[str, Any]:
        CAPABILITIES.ensure_supported(intent)
        market = self.market(intent.symbol)
        assert intent.limit_price is not None  # every supported type is priced
        if intent.order_type is OrderType.LIMIT:
            order_type: dict[str, Any] = {"limit": {"tif": _TIF[intent.time_in_force]}}
        else:
            assert intent.trigger_price is not None
            order_type = {
                "trigger": {
                    "isMarket": False,
                    "triggerPx": to_wire(intent.trigger_price),
                    "tpsl": "sl" if intent.order_type is OrderType.STOP_LIMIT else "tp",
                }
            }
        # Insertion order matters: it is the msgpack field order that gets hashed and signed.
        return {
            "a": market.asset,
            "b": intent.side is Side.BUY,
            "p": to_wire(intent.limit_price),
            "s": to_wire(intent.quantity),
            "r": intent.reduce_only,
            "t": order_type,
            "c": client_id,
        }

    def build_place(self, intent: OrderIntent, client_id: str) -> UnsignedRequest:
        action = {"type": "order", "orders": [self.order_wire(intent, client_id)], "grouping": "na"}
        return UnsignedRequest(
            Venue.HYPERLIQUID,
            Action.PLACE_ORDER,
            intent.account,
            intent.intent_id,
            client_id,
            intent.content_hash(),
            {"action": action},
        )

    def build_cancel(self, intent: OrderIntent, client_id: str) -> UnsignedRequest:
        market = self.market(intent.symbol)
        action = {"type": "cancelByCloid", "cancels": [{"asset": market.asset, "cloid": client_id}]}
        return UnsignedRequest(
            Venue.HYPERLIQUID,
            Action.CANCEL_ORDER,
            intent.account,
            intent.intent_id,
            client_id,
            intent.content_hash(),
            {"action": action},
        )
