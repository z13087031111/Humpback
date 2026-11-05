"""Aster V3 codec (pure: no network).

Facts encoded here (verified 2026-11-05 against ``asterdex/api-docs`` commit 10391d1):

* new integrations must use V3; Futures V1 was removed from the docs on 2026-11-02;
* order endpoint ``POST /fapi/v3/order``, cancel ``DELETE /fapi/v3/order``;
* ``newClientOrderId`` matches ``^[.A-Z:/a-z0-9_-]{1,36}$`` and is only unique among *open*
  orders, so Humpback keeps its own globally unique id and never relies on venue dedupe;
* TIF ``GTC``, ``IOC``, ``GTX`` (post-only); market and *_MARKET orders take no price and
  accept only ``GTC`` as TIF, so it is omitted;
* sending a parameter that does not apply to the order type is rejected by Aster, so the
  request includes only the parameters the type needs;
* nonce is a microsecond timestamp, valid ±60 s, last 100 tracked per agent address;
* HTTP 503 means the outcome is unknown and may have succeeded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from humpback.core.errors import UnsupportedCapability
from humpback.core.money import to_wire
from humpback.domain.models import OrderIntent, OrderType, TimeInForce, Venue
from humpback.venues.base import Action, UnsignedRequest, VenueCapabilities
from humpback.venues.precision import TickStepRules

CAPABILITIES = VenueCapabilities(
    venue=Venue.ASTER,
    order_types=frozenset(
        {
            OrderType.LIMIT,
            OrderType.MARKET,
            OrderType.STOP_LIMIT,
            OrderType.STOP_MARKET,
            OrderType.TAKE_PROFIT_LIMIT,
            OrderType.TAKE_PROFIT_MARKET,
        }
    ),
    time_in_force=frozenset({TimeInForce.GTC, TimeInForce.IOC, TimeInForce.POST_ONLY}),
    reduce_only=True,
    query_by_client_id=True,
    cancel_by_client_id=True,
    client_id_format="'hb' + 32 hex (34 chars)",
    signature_scheme="eip712 AsterSignTransaction v1 chainId 1666, urlencoded params as msg",
    nonce_unit="us",
    margin_modes=frozenset({"cross", "isolated"}),
    notes=("reduceOnly cannot be sent in hedge mode; Humpback assumes one-way mode",),
)

_TYPE = {
    OrderType.LIMIT: "LIMIT",
    OrderType.MARKET: "MARKET",
    OrderType.STOP_LIMIT: "STOP",
    OrderType.STOP_MARKET: "STOP_MARKET",
    OrderType.TAKE_PROFIT_LIMIT: "TAKE_PROFIT",
    OrderType.TAKE_PROFIT_MARKET: "TAKE_PROFIT_MARKET",
}
_TIF = {TimeInForce.GTC: "GTC", TimeInForce.IOC: "IOC", TimeInForce.POST_ONLY: "GTX"}
_MARKET_STYLE = frozenset({OrderType.MARKET, OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET})


@dataclass
class AsterCodec:
    rule_book: dict[str, TickStepRules] = field(default_factory=dict)

    @property
    def capabilities(self) -> VenueCapabilities:
        return CAPABILITIES

    def rules(self, symbol: str) -> TickStepRules:
        try:
            return self.rule_book[symbol]
        except KeyError:
            raise UnsupportedCapability(f"aster symbol {symbol} not configured") from None

    def encode_client_id(self, key: bytes) -> str:
        if len(key) != 16:
            raise ValueError("client key must be 16 bytes")
        return "hb" + key.hex()

    def order_params(self, intent: OrderIntent, client_id: str) -> dict[str, str]:
        CAPABILITIES.ensure_supported(intent)
        self.rules(intent.symbol)
        if intent.order_type in _MARKET_STYLE and intent.time_in_force is TimeInForce.POST_ONLY:
            raise UnsupportedCapability("aster market-style orders cannot be post-only")
        params: dict[str, str] = {
            "symbol": intent.symbol,
            "side": intent.side.value.upper(),
            "type": _TYPE[intent.order_type],
            "quantity": to_wire(intent.quantity),
        }
        if intent.limit_price is not None:
            params["price"] = to_wire(intent.limit_price)
        if intent.order_type not in _MARKET_STYLE:
            params["timeInForce"] = _TIF[intent.time_in_force]
        elif intent.time_in_force is TimeInForce.IOC and intent.order_type is not OrderType.MARKET:
            raise UnsupportedCapability("aster *_MARKET trigger orders accept only GTC")
        if intent.trigger_price is not None:
            params["stopPrice"] = to_wire(intent.trigger_price)
        if intent.reduce_only:
            params["reduceOnly"] = "true"
        params["newClientOrderId"] = client_id
        return params

    def build_place(self, intent: OrderIntent, client_id: str) -> UnsignedRequest:
        body: dict[str, Any] = {
            "method": "POST",
            "path": "/fapi/v3/order",
            "params": self.order_params(intent, client_id),
        }
        return UnsignedRequest(
            Venue.ASTER,
            Action.PLACE_ORDER,
            intent.account,
            intent.intent_id,
            client_id,
            intent.content_hash(),
            body,
        )

    def build_cancel(self, intent: OrderIntent, client_id: str) -> UnsignedRequest:
        body: dict[str, Any] = {
            "method": "DELETE",
            "path": "/fapi/v3/order",
            "params": {"symbol": intent.symbol, "origClientOrderId": client_id},
        }
        return UnsignedRequest(
            Venue.ASTER,
            Action.CANCEL_ORDER,
            intent.account,
            intent.intent_id,
            client_id,
            intent.content_hash(),
            body,
        )
