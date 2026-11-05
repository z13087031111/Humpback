import re
from decimal import Decimal

import pytest

from humpback.core.errors import PrecisionError, UnsupportedCapability
from humpback.core.ids import client_key
from humpback.domain.models import OrderIntent, OrderType, Side, TimeInForce, Venue
from humpback.venues.aster import AsterCodec
from humpback.venues.hyperliquid import HyperliquidCodec, HyperliquidMarket
from humpback.venues.lighter import LighterCodec, LighterMarket, check_api_key_index
from humpback.venues.precision import HyperliquidRules, LighterRules, TickStepRules


def make(
    venue: Venue,
    *,
    order_type: OrderType = OrderType.LIMIT,
    qty: str = "1",
    price: str | None = "100",
    trigger: str | None = None,
    tif: TimeInForce = TimeInForce.GTC,
    reduce_only: bool = False,
    symbol: str = "ETH",
) -> OrderIntent:
    return OrderIntent(
        intent_id="i",
        business_key="k",
        account="0xabc",
        venue=venue,
        symbol=symbol,
        side=Side.BUY,
        order_type=order_type,
        quantity=Decimal(qty),
        created_at_ms=0,
        trace_id="t",
        limit_price=None if price is None else Decimal(price),
        trigger_price=None if trigger is None else Decimal(trigger),
        time_in_force=tif,
        reduce_only=reduce_only,
    )


# ---------------------------------------------------------------- Hyperliquid

HL = HyperliquidCodec({"ETH": HyperliquidMarket("ETH", 1, HyperliquidRules(sz_decimals=4))})


def test_hyperliquid_rejects_market_orders_locally() -> None:
    with pytest.raises(UnsupportedCapability) as err:
        HL.build_place(
            make(Venue.HYPERLIQUID, order_type=OrderType.MARKET, price=None), "0x" + "0" * 32
        )
    assert err.value.code == "UNSUPPORTED_CAPABILITY"


def test_hyperliquid_order_wire_matches_sdk_shape() -> None:
    cloid = HL.encode_client_id(bytes(15) + b"\x01")
    assert cloid == "0x00000000000000000000000000000001"
    wire = HL.order_wire(make(Venue.HYPERLIQUID, qty="100", price="100.000"), cloid)
    assert list(wire) == ["a", "b", "p", "s", "r", "t", "c"]
    assert wire == {
        "a": 1,
        "b": True,
        "p": "100",
        "s": "100",
        "r": False,
        "t": {"limit": {"tif": "Gtc"}},
        "c": cloid,
    }
    post_only = HL.order_wire(make(Venue.HYPERLIQUID, tif=TimeInForce.POST_ONLY), cloid)
    assert post_only["t"] == {"limit": {"tif": "Alo"}}


def test_hyperliquid_trigger_and_cancel_wire() -> None:
    cloid = "0x" + "ab" * 16
    wire = HL.order_wire(
        make(Venue.HYPERLIQUID, order_type=OrderType.STOP_LIMIT, price="95", trigger="96.50"), cloid
    )
    assert wire["t"] == {"trigger": {"isMarket": False, "triggerPx": "96.5", "tpsl": "sl"}}
    cancel = HL.build_cancel(make(Venue.HYPERLIQUID), cloid)
    assert cancel.body == {
        "action": {"type": "cancelByCloid", "cancels": [{"asset": 1, "cloid": cloid}]}
    }


@pytest.mark.parametrize(
    ("sz_decimals", "spot", "price", "ok"),
    [
        (0, False, "123456", True),  # integer prices always allowed
        (0, False, "1234.5", True),  # 5 significant figures
        (0, False, "1234.56", False),  # 6 significant figures
        (0, False, "0.001234", True),  # 6 decimals allowed when szDecimals = 0
        (0, False, "0.0012345", False),
        (1, False, "0.01234", True),  # official example
        (1, False, "0.012345", False),  # official example
        (0, True, "0.0001234", True),  # spot: 8 - szDecimals
        (3, True, "0.0001234", False),
    ],
)
def test_hyperliquid_price_rules_follow_docs(
    sz_decimals: int, spot: bool, price: str, ok: bool
) -> None:
    rules = HyperliquidRules(sz_decimals=sz_decimals, is_spot=spot)
    if ok:
        rules.check_price(Decimal(price))
    else:
        with pytest.raises(PrecisionError):
            rules.check_price(Decimal(price))


def test_hyperliquid_size_and_min_value() -> None:
    rules = HyperliquidRules(sz_decimals=3)
    rules.check(make(Venue.HYPERLIQUID, qty="1.001", price="100"), None)
    with pytest.raises(PrecisionError, match="szDecimals"):
        rules.check(make(Venue.HYPERLIQUID, qty="1.0001", price="100"), None)
    with pytest.raises(PrecisionError, match="below"):
        rules.check(make(Venue.HYPERLIQUID, qty="0.05", price="100"), None)


# ---------------------------------------------------------------- Aster

ASTER_RULES = TickStepRules(
    tick_size=Decimal("0.01"),
    min_price=Decimal("0.01"),
    max_price=Decimal(1_000_000),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    max_qty=Decimal(1000),
    market_step_size=Decimal("0.01"),
    market_min_qty=Decimal("0.01"),
    market_max_qty=Decimal(100),
    min_notional=Decimal(5),
)
ASTER = AsterCodec({"ETHUSDT": ASTER_RULES})


def test_aster_client_id_fits_the_documented_pattern() -> None:
    cid = ASTER.encode_client_id(client_key("carry:ETH:2026-11-05"))
    assert len(cid) <= 36
    assert re.fullmatch(r"[\.A-Z\:/a-z0-9_-]{1,36}", cid)


def test_aster_params_only_carry_what_the_type_needs() -> None:
    limit = ASTER.order_params(
        make(Venue.ASTER, symbol="ETHUSDT", tif=TimeInForce.POST_ONLY), "hbX"
    )
    assert limit == {
        "symbol": "ETHUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "quantity": "1",
        "price": "100",
        "timeInForce": "GTX",
        "newClientOrderId": "hbX",
    }
    market = ASTER.order_params(
        make(
            Venue.ASTER, symbol="ETHUSDT", order_type=OrderType.MARKET, price=None, reduce_only=True
        ),
        "hbY",
    )
    assert "timeInForce" not in market and "price" not in market
    assert market["reduceOnly"] == "true"
    stop = ASTER.order_params(
        make(
            Venue.ASTER,
            symbol="ETHUSDT",
            order_type=OrderType.STOP_MARKET,
            price=None,
            trigger="90",
        ),
        "hbZ",
    )
    assert stop["type"] == "STOP_MARKET" and stop["stopPrice"] == "90"


def test_aster_filters() -> None:
    ASTER_RULES.check(make(Venue.ASTER, qty="0.123", price="100.01"), None)
    with pytest.raises(PrecisionError, match="tickSize"):
        ASTER_RULES.check(make(Venue.ASTER, qty="1", price="100.015"), None)
    with pytest.raises(PrecisionError, match="stepSize"):
        ASTER_RULES.check(make(Venue.ASTER, qty="0.1234", price="100"), None)
    with pytest.raises(PrecisionError, match="minNotional"):
        ASTER_RULES.check(make(Venue.ASTER, qty="0.01", price="100"), None)
    with pytest.raises(PrecisionError, match="stepSize"):  # market lot size differs
        ASTER_RULES.check(
            make(Venue.ASTER, order_type=OrderType.MARKET, price=None, qty="0.123"), Decimal(100)
        )


# ---------------------------------------------------------------- Lighter

LIGHTER = LighterCodec(
    {
        "ETH": LighterMarket(
            "ETH",
            0,
            LighterRules(
                size_decimals=4,
                price_decimals=2,
                min_base_amount=Decimal("0.005"),
                min_quote_amount=Decimal(10),
            ),
        )
    }
)


def test_lighter_reserved_key_slots_are_refused() -> None:
    for index in (0, 1, 2, 3, 255):
        with pytest.raises(ValueError, match="reserved"):
            check_api_key_index(index)
    assert check_api_key_index(4) == 4 and check_api_key_index(254) == 254


def test_lighter_integer_encoding() -> None:
    request = LIGHTER.build_place(
        make(Venue.LIGHTER, qty="0.05", price="3150", tif=TimeInForce.IOC), "123"
    )
    body = request.body
    assert body["base_amount"] == 500 and body["price"] == 315000
    assert body["time_in_force"] == 0 and body["order_expiry"] == 0
    assert body["client_order_index"] == 123
    with pytest.raises(PrecisionError):
        LIGHTER.build_place(make(Venue.LIGHTER, qty="0.00005", price="3150"), "1")


def test_lighter_client_index_is_uint48_and_never_zero() -> None:
    index = int(LIGHTER.encode_client_id(client_key("x")))
    assert 0 < index < 2**48
    with pytest.raises(ValueError):
        LIGHTER.encode_client_id(bytes(16))
