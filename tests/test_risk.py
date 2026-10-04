from datetime import datetime, timedelta

import pytest

from bot.risk_manager import RiskManager
from database.models import Trade
from tests.conftest import make_settings

NOW = datetime(2026, 10, 4, 15, 0, 0)


def closed_trade(pnl, exit_time, entry_time=None):
    return Trade(symbol="ETHUSDT", base_asset="ETH", mode="DRY_RUN", status="CLOSED", quantity=1,
                 entry_price=100, entry_quote=100, entry_time=entry_time or exit_time - timedelta(hours=1),
                 initial_stop=95, stop_price=95, highest_price=100, exit_price=100 + pnl,
                 exit_quote=100 + pnl, exit_time=exit_time, pnl_usdt=pnl)


@pytest.fixture
def rm(db):
    return RiskManager(make_settings(), db, "DRY_RUN")


def test_position_size_risk_per_trade(rm):
    # equity 10000, entry 100, stop 98 -> risk/unit = 2 + 100*0.003 = 2.3
    res = rm.position_size(10000, 10000, 100, 98)
    assert res.ok
    # 1% risk = 100 USDT -> qty=43.47 -> notional 4347 > 20% cap (2000) -> capped
    assert res.notional == pytest.approx(2000)
    assert res.risk_usdt <= 100 + 1e-9


def test_position_size_risk_limited_when_stop_wide(rm):
    # stop %20 uzakta: risk/unit = 20.3 -> qty = 100/20.3 = 4.926 -> notional 492.6 (< 2000 cap)
    res = rm.position_size(10000, 10000, 100, 80)
    assert res.notional == pytest.approx(100 / 20.3 * 100, rel=1e-6)
    assert res.risk_usdt == pytest.approx(100, rel=1e-6)  # tam %1 risk, maliyet dahil


def test_position_size_max_20_percent(rm):
    res = rm.position_size(1000, 1000, 100, 99.9)
    assert res.notional <= 1000 * 0.20 + 1e-9


def test_position_size_limited_by_free_usdt(rm):
    res = rm.position_size(10000, 150, 100, 98)
    assert res.notional < 150


def test_position_size_regime_multiplier(rm):
    full = rm.position_size(10000, 10000, 100, 98, 1.0)
    half = rm.position_size(10000, 10000, 100, 98, 0.5)
    assert half.notional == pytest.approx(full.notional / 2)
    assert not rm.position_size(10000, 10000, 100, 98, 0.0).ok


def test_position_size_invalid_stop(rm):
    assert not rm.position_size(1000, 1000, 100, 101).ok


def test_daily_loss_limit(rm, db):
    db.add_trade(closed_trade(-20, NOW - timedelta(hours=2)))
    assert not rm.daily_loss_exceeded(NOW, 1000, 0)  # -2%
    assert rm.daily_loss_exceeded(NOW, 1000, -10)  # -3% (gerçekleşmemiş dahil)
    check = rm.can_open_trade(NOW, 0, 1000, -10, False)
    assert not check.allowed and "Günlük zarar" in check.reason


def test_daily_loss_ignores_previous_days(rm, db):
    db.add_trade(closed_trade(-500, NOW - timedelta(days=1)))
    assert not rm.daily_loss_exceeded(NOW, 1000, 0)


def test_max_daily_trades(rm, db):
    for i in range(5):
        db.add_trade(closed_trade(1, NOW - timedelta(minutes=10 + i), entry_time=NOW - timedelta(hours=1, minutes=i)))
    check = rm.can_open_trade(NOW, 0, 1000, 0, False)
    assert not check.allowed and "Günlük maksimum işlem" in check.reason


def test_consecutive_losses_cooldown(rm, db):
    base = NOW - timedelta(hours=3)
    db.add_trade(closed_trade(5, base - timedelta(days=2)))
    for i in range(3):
        db.add_trade(closed_trade(-1, base - timedelta(days=1, hours=i)))
    # günlük işlem limitine takılmamak için kayıplar önceki güne yayıldı; sonuncu 3 saat önce
    db.add_trade(closed_trade(-1, base, entry_time=base - timedelta(days=1)))
    streak, _ = rm.consecutive_losses()
    assert streak == 4
    check = rm.can_open_trade(NOW, 0, 100000, 0, False)
    assert not check.allowed and "soğuma" in check.reason
    # 12 saat sonra tekrar izin
    later = base + timedelta(hours=12, minutes=1)
    assert rm.can_open_trade(later, 0, 100000, 0, False).allowed


def test_win_resets_streak(rm, db):
    for i in range(3):
        db.add_trade(closed_trade(-1, NOW - timedelta(days=1, hours=5 - i)))
    db.add_trade(closed_trade(2, NOW - timedelta(days=1)))
    assert rm.consecutive_losses()[0] == 0
    assert rm.can_open_trade(NOW, 0, 1000, 0, False).allowed


def test_emergency_and_max_open_positions(rm):
    assert not rm.can_open_trade(NOW, 0, 1000, 0, True).allowed
    check = rm.can_open_trade(NOW, 1, 1000, 0, False)
    assert not check.allowed and "açık pozisyon" in check.reason
