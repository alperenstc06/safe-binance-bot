from datetime import timedelta

import pytest

from bot.engine import BotEngine, SafetyError
from database.database import Database
from database.models import utcnow
from tests.conftest import FakeClient, make_settings


def dry_engine(fake=None, db=None, **kw):
    fake = fake or FakeClient()
    db = db or Database("sqlite:///:memory:")
    return BotEngine(make_settings(**kw), fake, db), fake, db


def live_engine(fake=None, db=None, **kw):
    fake = fake or FakeClient(has_keys=True)
    db = db or Database("sqlite:///:memory:")
    s = make_settings(trading_mode="LIVE", binance_api_key="k", binance_api_secret="s", **kw)
    return BotEngine(s, fake, db), fake, db


def test_dry_run_cycle_opens_position_with_stop():
    engine, fake, db = dry_engine()
    engine.run_cycle(force_scan=True)
    trades = db.open_trades("DRY_RUN")
    assert len(trades) == 1
    t = trades[0]
    assert t.symbol in ("BTCUSDT", "ETHUSDT")
    assert t.stop_price < t.entry_price and t.initial_stop == t.stop_price
    assert t.entry_quote <= 1000 * 0.20 * 1.01  # maks %20 pozisyon
    assert engine.orders.paper_usdt < 1000
    assert not fake.calls  # DRY_RUN'da borsaya emir gitmez
    # ikinci döngüde ikinci pozisyon açılmaz (maks 1)
    engine.run_cycle(force_scan=True)
    assert len(db.open_trades("DRY_RUN")) == 1
    assert "Maksimum açık pozisyon" in engine.no_trade_reason


def test_stop_loss_closes_position():
    engine, fake, db = dry_engine()
    engine.run_cycle(force_scan=True)
    t = db.open_trades("DRY_RUN")[0]
    fake.override_price[t.symbol] = t.stop_price * 0.99
    engine.run_cycle()
    closed = db.get_trade(t.id)
    assert closed.status == "CLOSED" and closed.exit_reason == "STOP_LOSS"
    assert closed.pnl_usdt < 0
    assert closed.exit_fee_usdt > 0 and closed.entry_fee_usdt > 0


def test_trailing_stop_moves_up_and_exits():
    engine, fake, db = dry_engine(rotation_enabled=False)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("DRY_RUN")[0]
    atr = t.current_atr
    fake.override_price[t.symbol] = t.entry_price + 4 * atr
    engine.run_cycle()
    t2 = db.get_trade(t.id)
    assert t2.trailing_active and t2.breakeven_active
    assert t2.stop_price > t.entry_price
    raised = t2.stop_price
    fake.override_price[t.symbol] = t.entry_price + 3 * atr  # geri çekilme, stop altına inmedi
    engine.run_cycle()
    t3 = db.get_trade(t.id)
    assert t3.status == "OPEN" and t3.stop_price == pytest.approx(raised)
    fake.override_price[t.symbol] = raised * 0.995
    engine.run_cycle()
    t4 = db.get_trade(t.id)
    assert t4.status == "CLOSED" and t4.exit_reason == "TRAILING_STOP"
    assert t4.pnl_usdt > 0


def test_no_trade_in_bear_regime():
    fake = FakeClient()
    from tests.conftest import make_klines
    fake.kl["BTCUSDT"] = make_klines(30000, up=0.003, down=-0.009)
    engine, _, db = dry_engine(fake)
    engine.run_cycle(force_scan=True)
    assert not db.open_trades("DRY_RUN")
    assert "BEAR" in engine.no_trade_reason


def test_no_trade_when_score_below_minimum():
    engine, fake, db = dry_engine(min_opportunity_score=99)
    engine.run_cycle(force_scan=True)
    assert not db.open_trades("DRY_RUN")
    assert "Uygun fırsat yok" in engine.no_trade_reason


def test_emergency_stop_blocks_trading_and_start():
    engine, fake, db = dry_engine()
    engine.emergency_stop()
    assert engine.emergency and not engine.running
    engine.run_cycle(force_scan=True)
    assert not db.open_trades("DRY_RUN")
    assert "Acil durdurma" in engine.no_trade_reason
    with pytest.raises(SafetyError):
        engine.start()
    engine.reset_emergency()
    assert not engine.emergency


def test_emergency_stop_persists_across_restart():
    db = Database("sqlite:///:memory:")
    engine, _, _ = dry_engine(db=db)
    engine.emergency_stop()
    engine2, _, _ = dry_engine(db=db)
    assert engine2.emergency


def test_close_all_positions():
    engine, fake, db = dry_engine()
    engine.run_cycle(force_scan=True)
    assert db.open_trades("DRY_RUN")
    results = engine.close_all_positions()
    assert results and not db.open_trades("DRY_RUN")
    assert db.recent_trades(1)[0].exit_reason == "MANUAL_CLOSE_ALL"


def test_restart_restores_open_position_dry_run():
    db = Database("sqlite:///:memory:")
    engine, fake, _ = dry_engine(db=db)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("DRY_RUN")[0]
    paper = engine.orders.paper_usdt
    engine2 = BotEngine(make_settings(), fake, db)
    msgs = engine2.sync_positions()
    assert any(t.symbol in m for m in msgs)
    assert engine2.orders.paper_usdt == pytest.approx(paper)
    engine2.run_cycle(force_scan=True)
    assert len(db.open_trades("DRY_RUN")) == 1  # yeni pozisyon açmadı, mevcut olanı yönetiyor
    assert db.open_trades("DRY_RUN")[0].id == t.id


def test_live_open_places_exchange_stop_order():
    engine, fake, db = live_engine()
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    kinds = [c[0] for c in fake.calls]
    assert kinds[:2] == ["BUY", "STOP"]
    assert t.stop_order_id is not None
    stop_call = fake.calls[1]
    assert float(stop_call[3]) <= t.stop_price  # tickSize'a aşağı yuvarlandı
    assert t.quantity == pytest.approx(float(fake.calls[0][2]) * 0.999)  # baz komisyon düşüldü


def test_live_closes_if_stop_cannot_be_placed():
    fake = FakeClient(has_keys=True)
    fake.fail_stop = True
    engine, _, db = live_engine(fake)
    engine.run_cycle(force_scan=True)
    assert not db.open_trades("LIVE")
    assert db.recent_trades(1, "LIVE")[0].exit_reason == "STOP_PLACEMENT_FAILED"


def test_live_safety_refuses_withdrawal_permission():
    fake = FakeClient(has_keys=True)
    fake.permissions["enableWithdrawals"] = True
    engine, _, _ = live_engine(fake)
    with pytest.raises(SafetyError):
        engine.start()
    assert not engine.running


def test_restart_sync_live_stop_filled_while_offline():
    db = Database("sqlite:///:memory:")
    engine, fake, _ = live_engine(db=db)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    fake.fill_stop(int(t.stop_order_id), t.stop_price)
    engine2 = BotEngine(engine.s, fake, db)
    engine2.sync_positions()
    closed = db.get_trade(t.id)
    assert closed.status == "CLOSED" and closed.exit_reason == "STOP_LOSS_EXCHANGE"


def test_restart_sync_live_asset_missing_marks_external_close():
    db = Database("sqlite:///:memory:")
    engine, fake, _ = live_engine(db=db)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    fake.orders[int(t.stop_order_id)]["status"] = "CANCELED"
    fake.balances_data[t.base_asset] = {"free": 0.0, "locked": 0.0}
    engine2 = BotEngine(engine.s, fake, db)
    engine2.sync_positions()
    closed = db.get_trade(t.id)
    assert closed.status == "CLOSED" and closed.exit_reason == "EXTERNAL_CLOSE"


def test_restart_sync_live_replaces_missing_stop_order():
    db = Database("sqlite:///:memory:")
    engine, fake, _ = live_engine(db=db)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    old = int(t.stop_order_id)
    fake.cancel_order(t.symbol, old)  # stop dışarıdan iptal edildi
    engine2 = BotEngine(engine.s, fake, db)
    engine2.sync_positions()
    t2 = db.get_trade(t.id)
    assert t2.status == "OPEN" and t2.stop_order_id is not None and int(t2.stop_order_id) != old


def test_live_trailing_replaces_exchange_stop():
    engine, fake, db = live_engine(rotation_enabled=False)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    old = t.stop_order_id
    fake.override_price[t.symbol] = t.entry_price + 4 * t.current_atr
    engine.run_cycle()
    t2 = db.get_trade(t.id)
    assert t2.stop_order_id != old and t2.stop_price > t.stop_price
    assert fake.orders[int(old)]["status"] == "CANCELED"


def test_live_existing_holdings_not_sold_by_default():
    fake = FakeClient(has_keys=True)
    from tests.conftest import make_klines
    fake.balances_data["SOL"] = {"free": 10.0, "locked": 0.0}
    engine, _, db = live_engine(fake)
    engine.run_cycle(force_scan=True)
    sol = [d for d in engine.holding_decisions if d.asset == "SOL"]
    assert sol and sol[0].decision in ("SELL", "PARTIAL_SELL")
    assert not sol[0].executable
    assert not any(c[0] == "SELL" and c[1] == "SOLUSDT" for c in fake.calls)
    assert fake.balances_data["SOL"]["free"] == 10.0


def test_daily_stats_recorded():
    engine, fake, db = dry_engine()
    engine.run_cycle(force_scan=True)
    day = utcnow().strftime("%Y-%m-%d")
    stat = db.get_daily_stat(day)
    assert stat is not None and stat.trades_opened == 1 and stat.start_equity == pytest.approx(1000)


def test_rotation_requires_large_score_gap():
    engine, fake, db = dry_engine(rotation_min_hold_minutes=0)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("DRY_RUN")[0]
    # Diğer aday benzer puanda -> rotasyon olmamalı
    engine.run_cycle(force_scan=True)
    assert db.get_trade(t.id).status == "OPEN"
    t.entry_time = utcnow() - timedelta(hours=2)
    db.save_trade(t)
    engine.run_cycle(force_scan=True)
    assert db.get_trade(t.id).status == "OPEN"


def test_rotation_happens_on_large_score_gap():
    engine, fake, db = dry_engine(rotation_min_hold_minutes=0)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("DRY_RUN")[0]
    other = "ETHUSDT" if t.symbol == "BTCUSDT" else "BTCUSDT"
    # Tutulan coinin likiditesi bozuldu: puanı ~15 düşer
    fake.quote_volume[t.symbol] = 1e6
    fake.spread[t.symbol] = 0.003
    engine.run_cycle(force_scan=True)
    old = db.get_trade(t.id)
    assert old.status == "CLOSED" and old.exit_reason == "ROTATION"
    new = db.open_trades("DRY_RUN")
    assert len(new) == 1 and new[0].symbol == other


def test_account_status_reports_real_portfolio_and_errors():
    from binance_client.client import BinanceAPIError
    fake = FakeClient(has_keys=True)
    fake.balances_data["ETH"] = {"free": 0.001, "locked": 0.0}
    engine, _, db = dry_engine(fake)
    engine.run_cycle(force_scan=True)
    st = engine.status()
    assert st["account_status"] == {"has_api_keys": True, "error": None}
    assets = {a["asset"] for a in st["real_portfolio"]["assets"]}
    assert {"USDT", "ETH"} <= assets

    def broken():
        raise BinanceAPIError("Binance hata -2015: Invalid API-key, IP, or permissions for action.")
    fake.balances = broken
    engine.run_cycle(force_scan=True)
    st = engine.status()
    assert "Invalid API-key" in st["account_status"]["error"]
    assert any(l.category == "ACCOUNT" for l in db.recent_logs(20))


def test_live_stop_quantity_capped_to_real_balance():
    """Komisyon baz varlıktan kesilip bakiye kayıttan az kalırsa stop gerçek bakiyeyle konur."""
    engine, fake, db = live_engine(rotation_enabled=False)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    fake.cancel_order(t.symbol, int(t.stop_order_id))
    t.stop_order_id = None
    db.save_trade(t)
    base = t.base_asset
    fake.balances_data[base]["free"] = t.quantity * 0.998  # gerçek bakiye kayıttan az
    real_stop = fake.stop_loss_limit_sell

    def strict_stop(symbol, quantity, stop_price, limit_price):
        if float(quantity) > fake.balances_data[base]["free"] + 1e-12:
            from binance_client.client import BinanceAPIError
            raise BinanceAPIError("Binance TR hata 2202: Insufficient balance")
        return real_stop(symbol, quantity, stop_price, limit_price)
    fake.stop_loss_limit_sell = strict_stop
    engine.run_cycle()
    t2 = db.get_trade(t.id)
    assert t2.status == "OPEN" and t2.stop_order_id is not None
    assert t2.quantity <= t.quantity * 0.998 + 1e-12


def test_restart_sync_reduces_quantity_for_small_shortfall():
    db = Database("sqlite:///:memory:")
    engine, fake, _ = live_engine(db=db)
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE")[0]
    fake.cancel_order(t.symbol, int(t.stop_order_id))
    fake.balances_data[t.base_asset]["free"] = t.quantity * 0.999
    engine2 = BotEngine(engine.s, fake, db)
    engine2.sync_positions()
    t2 = db.get_trade(t.id)
    assert t2.status == "OPEN" and t2.quantity <= t.quantity * 0.999 + 1e-12
    assert t2.stop_order_id is not None
