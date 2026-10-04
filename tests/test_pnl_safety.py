"""Hatalı PNL olayının (eski toz bakiye + gecikmeli bakiye) ve ilgili senaryoların testleri."""

import pytest

from binance_client.client import BinanceAPIError
from bot.engine import MAX_UNVERIFIED_STOP_CYCLES, BotEngine, ReviewRequired
from database.database import Database
from database.models import Trade, utcnow
from tests.conftest import FakeClient, make_settings


def live_engine(fake=None, db=None, **kw):
    fake = fake or FakeClient(has_keys=True)
    db = db or Database("sqlite:///:memory:")
    s = make_settings(trading_mode="LIVE", binance_api_key="k", binance_api_secret="s",
                      rotation_enabled=False, **kw)
    return BotEngine(s, fake, db), fake, db


class LaggingBalances:
    """Alımdan sonra baz varlık bakiyesini 'eski toz' olarak gösterir (borsa gecikmesi)."""

    def __init__(self, fake, dust=0.00042):
        self.fake = fake
        self.dust = dust
        self.asset = None
        self.lag = True
        self._orig_buy = fake.market_buy
        self._orig_bal = fake.balances
        fake.market_buy = self.buy
        fake.balances = self.balances

    def buy(self, symbol, quantity):
        resp = self._orig_buy(symbol, quantity)
        self.asset = symbol[:-4]
        return resp

    def balances(self):
        out = self._orig_bal()
        if self.asset and self.lag:
            out[self.asset] = {"free": self.dust, "locked": 0.0}
        return out


def bought_qty(fake):
    return float(next(c[2] for c in fake.calls if c[0] == "BUY"))


def test_incident_stale_dust_does_not_shrink_quantity_or_book_loss():
    engine, fake, db = live_engine()
    lag = LaggingBalances(fake)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    qty = bought_qty(fake)
    assert t.quantity == pytest.approx(qty * 0.999)  # gerçekleşme raporu (komisyon düşülmüş)
    assert t.entry_quote == pytest.approx(qty * t.entry_price, rel=1e-6)
    assert t.stop_order_id is None  # bakiye doğrulanmadan kısmi stop konmadı
    assert not db.closed_trades_desc(mode="LIVE")
    assert db.total_realized_pnl("LIVE") == 0
    assert not any(c[0] == "SELL" for c in fake.calls)  # panik satış yok
    # bakiye yansıyınca stop tam miktarla konur, miktar/maliyet değişmez
    lag.lag = False
    engine.run_cycle()
    t2 = db.get_trade(t.id)
    assert t2.status == "OPEN" and t2.stop_order_id is not None
    assert t2.quantity == pytest.approx(t.quantity) and t2.entry_quote == pytest.approx(t.entry_quote)
    stop = fake.orders[int(t2.stop_order_id)]
    assert float(stop["origQty"]) == pytest.approx(t2.quantity, rel=1e-2)  # stepSize yuvarlaması


def test_persistent_unverified_balance_flags_review_and_blocks_new_trades():
    engine, fake, db = live_engine()
    LaggingBalances(fake)
    for _ in range(MAX_UNVERIFIED_STOP_CYCLES + 1):
        engine.run_cycle(force_scan=True)
    st = engine.status()
    assert st["review_required"]
    assert "İnceleme gerekiyor" in engine.no_trade_reason or len(db.open_trades("LIVE")) == 1
    assert len(db.open_trades("LIVE")) == 1
    assert not db.closed_trades_desc(mode="LIVE")
    engine.clear_review()
    assert not engine.review_flags()


def test_close_with_partial_sale_keeps_remainder_and_proportional_cost():
    engine, fake, db = live_engine()
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    qty0, cost0 = t.quantity, t.entry_quote
    fake.cancel_order(t.symbol, int(t.stop_order_id))
    t.stop_order_id = None
    db.save_trade(t)
    fake.balances_data[t.base_asset]["free"] = qty0 / 2  # yalnızca yarısı satılabilir
    assert engine.close_trade(t, "MANUAL_CLOSE_ALL") is False
    rest = db.get_trade(t.id)
    assert rest.status == "OPEN"
    assert rest.entry_quote == pytest.approx(cost0 * rest.quantity / qty0, rel=1e-6)
    part = [x for x in db.closed_trades_desc(mode="LIVE") if x.exit_reason.startswith("PARTIAL")]
    assert len(part) == 1 and abs(part[0].pnl_usdt) < cost0 * 0.05  # tüm maliyet zarar yazılmadı
    assert db.total_realized_pnl("LIVE") > -cost0 * 0.05


def test_stop_partially_filled_then_gone_records_partial_and_replaces_stop():
    engine, fake, db = live_engine()
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    qty0, cost0 = t.quantity, t.entry_quote
    oid = int(t.stop_order_id)
    o = fake.orders[oid]
    half = float(o["origQty"]) / 2
    price = float(o["price"])
    o.update(status="CANCELED", executedQty=str(half), cummulativeQuoteQty=str(half * price))
    b = fake.balances_data[t.base_asset]
    b["locked"] -= float(o["origQty"])
    b["free"] += float(o["origQty"]) - half
    engine.run_cycle()
    rest = db.get_trade(t.id)
    assert rest.status == "OPEN"
    assert rest.quantity == pytest.approx(qty0 - half, rel=1e-3)
    assert rest.entry_quote == pytest.approx(cost0 * rest.quantity / qty0, rel=1e-3)
    assert rest.stop_order_id is not None and int(rest.stop_order_id) != oid
    part = [x for x in db.closed_trades_desc(mode="LIVE") if x.exit_reason.startswith("PARTIAL")]
    assert len(part) == 1 and part[0].quantity == pytest.approx(half)


def test_inconsistent_record_is_never_auto_closed():
    engine, fake, db = live_engine()
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    fake.cancel_order(t.symbol, int(t.stop_order_id))
    t.stop_order_id = None
    t.quantity = t.quantity / 10000  # eski hatadaki gibi: miktar küçük, maliyet büyük
    db.save_trade(t)
    sells_before = sum(1 for c in fake.calls if c[0] == "SELL")
    with pytest.raises(ReviewRequired):
        engine.close_trade(t, "STOP_PLACEMENT_FAILED")
    assert db.get_trade(t.id).status == "OPEN"
    assert engine.review_flags()
    assert sum(1 for c in fake.calls if c[0] == "SELL") == sells_before
    assert db.total_realized_pnl("LIVE") == 0


def test_restart_with_large_shortfall_splits_estimated_external_sale():
    db = Database("sqlite:///:memory:")
    engine, fake, _ = live_engine(db=db)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    qty0, cost0 = t.quantity, t.entry_quote
    fake.cancel_order(t.symbol, int(t.stop_order_id))
    fake.balances_data[t.base_asset]["free"] = qty0 * 0.5  # yarısı elle satılmış
    engine2 = BotEngine(engine.s, fake, db)
    engine2.sync_positions()
    rest = db.get_trade(t.id)
    assert rest.status == "OPEN" and rest.quantity == pytest.approx(qty0 * 0.5, rel=1e-2)
    assert rest.entry_quote == pytest.approx(cost0 * 0.5, rel=1e-2)
    assert rest.stop_order_id is not None
    part = [x for x in db.closed_trades_desc(mode="LIVE") if x.exit_reason == "PARTIAL_EXTERNAL"]
    assert len(part) == 1 and abs(part[0].pnl_usdt) < cost0 * 0.05
    assert engine2.risk.trades_today(utcnow()) == 1  # kısmi kayıt yeni işlem sayılmaz


def test_restart_with_unreadable_stop_status_keeps_existing_order():
    db = Database("sqlite:///:memory:")
    engine, fake, _ = live_engine(db=db)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    oid = t.stop_order_id

    def broken(symbol, order_id):
        raise BinanceAPIError("Binance isteği başarısız: timeout")
    fake.get_order = broken
    stops_before = sum(1 for c in fake.calls if c[0] == "STOP")
    engine2 = BotEngine(engine.s, fake, db)
    engine2.sync_positions()
    engine2.run_cycle()
    t2 = db.get_trade(t.id)
    assert t2.status == "OPEN" and t2.stop_order_id == oid
    assert sum(1 for c in fake.calls if c[0] == "STOP") == stops_before  # ikinci stop yok


def test_true_dust_close_uses_estimated_value_not_zero():
    engine, fake, db = live_engine()
    price = fake.last_price("ETHUSDT")
    qty = 0.0012  # ~ birkaç USDT: MIN_NOTIONAL (5) altında
    t = db.add_trade(Trade(symbol="ETHUSDT", base_asset="ETH", mode="LIVE", status="OPEN", quantity=qty,
                           entry_price=price, entry_quote=qty * price, initial_stop=price * 0.9,
                           stop_price=price * 0.9, highest_price=price))
    fake.balances_data["ETH"] = {"free": qty, "locked": 0.0}
    assert engine.close_trade(t, "MANUAL_CLOSE_ALL") is True
    closed = db.get_trade(t.id)
    assert closed.status == "CLOSED" and closed.exit_quote > 0
    assert closed.pnl_usdt > -qty * price * 0.05


def test_commission_rounding_difference_adjusts_quantity_keeps_cost():
    engine, fake, db = live_engine()
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    cost0 = t.entry_quote
    fake.cancel_order(t.symbol, int(t.stop_order_id))
    t.stop_order_id = None
    db.save_trade(t)
    fake.balances_data[t.base_asset]["free"] = t.quantity * 0.995  # %0.5 fark: komisyon/yuvarlama
    engine.run_cycle()
    t2 = db.get_trade(t.id)
    assert t2.stop_order_id is not None
    assert t2.quantity <= t.quantity * 0.995 + 1e-12
    assert t2.entry_quote == pytest.approx(cost0)


def test_stop_filter_failure_reason_is_logged_and_sync_message_honest():
    db = Database("sqlite:///:memory:")
    engine, fake, _ = live_engine(db=db)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    fake.cancel_order(t.symbol, int(t.stop_order_id))
    # fiyat filtresi stopu reddetsin (minPrice stopun üstünde)
    for info in fake.infos:
        if info["symbol"] == t.symbol:
            info["filters"][0]["minPrice"] = str(t.stop_price * 2)
    engine2 = BotEngine(engine.s, fake, db)
    msgs = engine2.sync_positions()
    assert any("KONAMADI" in m for m in msgs)
    assert any("borsa stop emri konamadı" in l.message and "minPrice" in l.message
               for l in db.recent_logs(20))
    assert db.get_trade(t.id).stop_order_id is None


def test_exchange_stop_not_churned_for_tiny_moves():
    engine, fake, db = live_engine()
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    atr = t.current_atr
    fake.override_price[t.symbol] = t.entry_price + 4 * atr  # trailing aktif, stop yükselir
    engine.run_cycle()
    t1 = db.get_trade(t.id)
    stops_after_move = sum(1 for c in fake.calls if c[0] == "STOP")
    # fiyat çok az yükselir: yazılım stopu yükselir ama borsa emri yenilenmez
    fake.override_price[t.symbol] = (t.entry_price + 4 * atr) * 1.0002
    engine.run_cycle()
    t2 = db.get_trade(t.id)
    assert t2.stop_price > t1.stop_price
    assert t2.stop_order_id == t1.stop_order_id
    assert sum(1 for c in fake.calls if c[0] == "STOP") == stops_after_move
    # anlamlı yükselişte yenilenir
    fake.override_price[t.symbol] = (t.entry_price + 4 * atr) * 1.02
    engine.run_cycle()
    assert db.get_trade(t.id).stop_order_id != t1.stop_order_id
