import pytest

from bot.order_manager import OrderManager, parse_market_fill
from bot.strategy import expected_edge
from tests.conftest import make_settings


def lookup(symbol):
    return {"BNBUSDT": 500.0}[symbol]


def test_buy_commission_in_base_asset_reduces_quantity():
    resp = {"orderId": 1, "executedQty": "1.0", "cummulativeQuoteQty": "2000",
            "fills": [{"price": "2000", "qty": "1.0", "commission": "0.001", "commissionAsset": "ETH"}]}
    f = parse_market_fill(resp, "BUY", "ETH", lookup)
    assert f.quantity == pytest.approx(0.999)
    assert f.quote_net == pytest.approx(2000)
    assert f.fee_usdt == pytest.approx(2.0)
    assert f.external_fee_usdt == 0


def test_bnb_commission_is_external_fee():
    resp = {"orderId": 2, "executedQty": "1.0", "cummulativeQuoteQty": "2000",
            "fills": [{"price": "2000", "qty": "1.0", "commission": "0.003", "commissionAsset": "BNB"}]}
    f = parse_market_fill(resp, "BUY", "ETH", lookup)
    assert f.quantity == pytest.approx(1.0)
    assert f.external_fee_usdt == pytest.approx(1.5)
    assert f.fee_usdt == pytest.approx(1.5)


def test_sell_commission_in_usdt_reduces_proceeds():
    resp = {"orderId": 3, "executedQty": "1.0", "cummulativeQuoteQty": "2100",
            "fills": [{"price": "2100", "qty": "1.0", "commission": "2.1", "commissionAsset": "USDT"}]}
    f = parse_market_fill(resp, "SELL", "ETH", lookup)
    assert f.quote_net == pytest.approx(2097.9)
    assert f.fee_usdt == pytest.approx(2.1)


def test_paper_buy_and_sell_charge_fee_and_slippage(db, fake):
    s = make_settings()
    om = OrderManager(fake, db, s)
    filters = fake.get_filters("ETHUSDT")
    start = om.paper_usdt
    buy = om.buy("ETHUSDT", 0.1, filters)
    ask = float(fake.book_ticker("ETHUSDT")["askPrice"])
    assert buy.avg_price == pytest.approx(ask * (1 + s.slippage_pct))
    notional = buy.quantity * buy.avg_price
    assert buy.fee_usdt == pytest.approx(notional * s.fee_rate)
    assert om.paper_usdt == pytest.approx(start - notional - buy.fee_usdt)
    sell = om.sell("ETHUSDT", buy.quantity, filters)
    assert sell.fee_usdt == pytest.approx(sell.quantity * sell.avg_price * s.fee_rate)
    # aynı fiyatta al-sat: komisyon + slippage + spread kadar zarar
    assert om.paper_usdt < start
    loss = start - om.paper_usdt
    assert loss == pytest.approx(buy.fee_usdt + sell.fee_usdt + notional - sell.quantity * sell.avg_price, rel=1e-6)


def test_paper_buy_insufficient_balance(db, fake):
    from bot.order_manager import OrderError
    om = OrderManager(fake, db, make_settings(dry_run_start_balance=10))
    with pytest.raises(OrderError):
        om.buy("ETHUSDT", 1.0, fake.get_filters("ETHUSDT"))


def test_expected_edge_requires_net_advantage_after_costs():
    good = expected_edge(85, 100, 97, 2.0, 0.003, 0.003, 3.0)
    assert good.ok and good.expected_net_pct > 0.003
    tiny = expected_edge(85, 100, 99.8, 2.0, 0.003, 0.003, 3.0)  # hedef %0.4 < 3x maliyet
    assert not tiny.ok and "maliyet" in tiny.reason
    expensive = expected_edge(75, 100, 98, 2.0, 0.02, 0.003, 1.0)
    assert not expensive.ok
