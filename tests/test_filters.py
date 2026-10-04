from decimal import Decimal

import pytest

from binance_client.filters import FilterError, SymbolFilters, fmt_decimal


def make_filters(**kw):
    info = {
        "symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT", "status": "TRADING",
        "orderTypes": ["MARKET", "STOP_LOSS_LIMIT"],
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000", "tickSize": kw.get("tick", "0.01")},
            {"filterType": "LOT_SIZE", "minQty": kw.get("min_qty", "0.0001"), "maxQty": "9000", "stepSize": kw.get("step", "0.0001")},
            {"filterType": "MIN_NOTIONAL", "minNotional": kw.get("min_notional", "10"), "applyToMarket": True},
        ],
    }
    if "market_step" in kw:
        info["filters"].append({"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "100",
                                "stepSize": kw["market_step"]})
    return SymbolFilters.from_exchange_info(info)


def test_parses_filters():
    f = make_filters()
    assert f.tick_size == Decimal("0.01")
    assert f.step_size == Decimal("0.0001")
    assert f.min_qty == Decimal("0.0001")
    assert f.min_notional == Decimal("10")


def test_lot_size_rounds_down_to_step():
    f = make_filters(step="0.001")
    assert f.round_qty(1.23456789) == Decimal("1.234")
    assert f.round_qty(0.0009) == Decimal("0")


def test_lot_size_max_qty_cap():
    f = make_filters()
    assert f.round_qty(10000) == Decimal("9000")


def test_lot_size_min_qty_rejected():
    f = make_filters(min_qty="0.01", min_notional="0")
    with pytest.raises(FilterError):
        f.validate_order(Decimal("0.005"), Decimal("2000"))


def test_step_violation_rejected():
    f = make_filters(step="0.01", min_notional="0")
    with pytest.raises(FilterError):
        f.validate_order(Decimal("1.005"), Decimal("100"))


def test_min_notional_rejected_and_accepted():
    f = make_filters(min_notional="10")
    with pytest.raises(FilterError):
        f.validate_order(Decimal("0.004"), Decimal("2000"))  # 8 USDT
    f.validate_order(Decimal("0.006"), Decimal("2000"))  # 12 USDT


def test_notional_filter_type_supported():
    info = {"symbol": "X", "baseAsset": "X", "quoteAsset": "USDT",
            "filters": [{"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "100",
                         "applyMinToMarket": True}]}
    f = SymbolFilters.from_exchange_info(info)
    assert f.min_notional == Decimal("5") and f.max_notional == Decimal("100")
    with pytest.raises(FilterError):
        f.validate_order(Decimal("1"), Decimal("200"))


def test_tick_size_rounding():
    f = make_filters(tick="0.05")
    assert f.round_price(123.4567) == Decimal("123.45")
    assert f.round_price(123.4999) == Decimal("123.45")
    assert f.round_price_up(123.41) == Decimal("123.45")
    f.validate_price(Decimal("123.45"))
    with pytest.raises(FilterError):
        f.validate_price(Decimal("123.42"))


def test_market_lot_size_is_stricter_for_market_orders():
    f = make_filters(step="0.0001", market_step="0.001")
    assert f.round_qty(1.23456, market=True) == Decimal("1.234")
    assert f.round_qty(1.23456, market=False) == Decimal("1.2345")


def test_prepare_order_and_format():
    f = make_filters()
    q, p = f.prepare_order(0.123456, 2000.129)
    assert q == Decimal("0.1234") and p == Decimal("2000.12")
    assert fmt_decimal(Decimal("1E-5")) == "0.00001"
    assert fmt_decimal(Decimal("10.500")) == "10.5"
