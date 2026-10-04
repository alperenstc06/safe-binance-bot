import pytest

from bot.market_regime import Regime, detect_regime, regime_adjustments
from bot.strategy import Candles, atr, ema, rsi, score_symbol, trend_broken, momentum_negative, compute_indicators
from tests.conftest import make_klines


def test_ema_converges_to_constant():
    assert ema([10.0] * 50, 20)[-1] == pytest.approx(10.0)


def test_rsi_bounds():
    up = [float(i) for i in range(1, 60)]
    assert rsi(up)[-1] == pytest.approx(100.0)
    down = list(reversed(up))
    assert rsi(down)[-1] == pytest.approx(0.0)
    vals = rsi([100 + (i % 3) for i in range(60)])
    assert all(0 <= v <= 100 for v in vals)


def test_atr_positive():
    a = atr([11, 12, 13], [9, 10, 11], [10, 11, 12], period=2)
    assert a[-1] > 0


def test_uptrend_scores_high_downtrend_low():
    up = Candles.from_klines(make_klines(100))
    down = Candles.from_klines(make_klines(100, up=0.003, down=-0.009))
    s_up = score_symbol("UP", up, 300e6, 0.0001, "BULL")
    s_down = score_symbol("DN", down, 300e6, 0.0001, "BULL")
    assert s_up.score >= 75 and s_up.eligible
    assert s_down.score < 50
    assert 0 <= s_down.score <= 100


def test_short_history_not_eligible():
    c = Candles.from_klines(make_klines(100, n=120))
    assert not score_symbol("X", c, 300e6, 0.0001, "BULL").eligible


def test_pump_not_eligible():
    kl = make_klines(100)
    last = kl[-1]
    o = float(last[1])
    kl[-1] = [last[0], last[1], f"{o * 1.2:.8f}", last[3], f"{o * 1.15:.8f}", "1", last[6], last[7]]
    res = score_symbol("PUMP", Candles.from_klines(kl), 300e6, 0.0001, "BULL")
    assert not res.eligible


def test_exit_signals_on_downtrend():
    ind = compute_indicators(Candles.from_klines(make_klines(100, up=0.003, down=-0.012)))
    assert trend_broken(ind)


def test_regime_detection():
    bull = detect_regime(Candles.from_klines(make_klines(30000)), 1.0)
    assert bull.regime == Regime.BULL and bull.allows_new_trades
    bear = detect_regime(Candles.from_klines(make_klines(30000, up=0.003, down=-0.009)), -2.0)
    assert bear.regime == Regime.BEAR and not bear.allows_new_trades
    hv = detect_regime(Candles.from_klines(make_klines(30000)), 12.0)
    assert hv.regime == Regime.HIGH_VOLATILITY and not hv.allows_new_trades


def test_regime_adjustments():
    assert regime_adjustments(Regime.BULL, 0.5, 5) == (1.0, 0.0)
    assert regime_adjustments(Regime.NEUTRAL, 0.5, 5) == (0.5, 5)
    assert regime_adjustments(Regime.BEAR, 0.5, 5)[0] == 0.0
    assert regime_adjustments(Regime.HIGH_VOLATILITY, 0.5, 5)[0] == 0.0
