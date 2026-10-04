import pytest

from bot.risk_manager import compute_stop_update, initial_stop_price, stop_hit

COST = 0.003


def upd(entry, stop, highest, price, atr=2.0, be=False, tr=False):
    return compute_stop_update(entry, stop, highest, price, atr, be, tr, 1.0, 1.5, 2.0, COST)


def test_initial_stop_atr_based():
    assert initial_stop_price(100, 2, 2.0) == pytest.approx(96)
    with pytest.raises(ValueError):
        initial_stop_price(100, 60, 2.0)


def test_stop_hit():
    assert stop_hit(95.9, 96)
    assert stop_hit(96, 96)
    assert not stop_hit(96.1, 96)


def test_stop_never_widens_on_price_drop():
    r = upd(100, 96, 100, 97)
    assert r.stop == 96 and not r.changed


def test_stop_never_widens_with_larger_atr():
    r = upd(100, 99, 104, 103, atr=10, be=True, tr=True)
    assert r.stop == 99  # trailing 104-20=84 < 99 -> değişmez


def test_breakeven_after_one_atr():
    r = upd(100, 96, 100, 102.1)
    assert r.breakeven_active and not r.trailing_active
    assert r.stop == pytest.approx(100 * (1 + COST))
    assert r.changed


def test_trailing_after_trigger_and_follows_high():
    r = upd(100, 96, 100, 103.5)
    assert r.trailing_active
    assert r.stop == pytest.approx(max(100.3, 103.5 - 4))
    r2 = upd(100, r.stop, r.highest, 110, be=True, tr=True)
    assert r2.stop == pytest.approx(106)
    # fiyat geri çekilirse stop aşağı inmez
    r3 = upd(100, r2.stop, r2.highest, 107, be=True, tr=True)
    assert r3.stop == pytest.approx(106) and r3.highest == 110
    assert stop_hit(105.9, r3.stop)
