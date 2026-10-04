"""BTC tabanlı genel piyasa rejimi: BULL / NEUTRAL / BEAR / HIGH_VOLATILITY."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from bot.strategy import Candles, compute_indicators


class Regime(str, Enum):
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"


@dataclass
class RegimeResult:
    regime: Regime
    reason: str
    btc_price: float = 0.0
    atr_pct: float = 0.0
    change_24h_pct: float = 0.0

    @property
    def allows_new_trades(self) -> bool:
        return self.regime in (Regime.BULL, Regime.NEUTRAL)


def detect_regime(
    candles: Candles,
    change_24h_pct: float,
    high_vol_atr_pct: float = 0.04,
    high_vol_24h_change_pct: float = 8.0,
) -> RegimeResult:
    if len(candles) < 50:
        return RegimeResult(Regime.NEUTRAL, "BTC verisi yetersiz; temkinli NEUTRAL")
    ind = compute_indicators(candles)
    if ind.atr_pct >= high_vol_atr_pct or abs(change_24h_pct) >= high_vol_24h_change_pct:
        return RegimeResult(Regime.HIGH_VOLATILITY,
                            f"BTC ATR%={ind.atr_pct * 100:.2f}, 24s değişim={change_24h_pct:.2f}%",
                            ind.close, ind.atr_pct, change_24h_pct)
    if ind.close > ind.ema200 and ind.ema50 > ind.ema200 and ind.ema20 > ind.ema50:
        return RegimeResult(Regime.BULL, "BTC EMA20 > EMA50 > EMA200 ve fiyat EMA200 üstünde",
                            ind.close, ind.atr_pct, change_24h_pct)
    if ind.close < ind.ema200 and ind.ema50 < ind.ema200:
        return RegimeResult(Regime.BEAR, "BTC fiyatı ve EMA50, EMA200 altında",
                            ind.close, ind.atr_pct, change_24h_pct)
    return RegimeResult(Regime.NEUTRAL, "BTC belirgin trend göstermiyor",
                        ind.close, ind.atr_pct, change_24h_pct)


def regime_adjustments(regime: Regime, neutral_size_multiplier: float,
                       neutral_extra_score: float) -> tuple[float, float]:
    """(pozisyon boyutu çarpanı, minimum puana eklenecek değer)."""
    if regime == Regime.BULL:
        return 1.0, 0.0
    if regime == Regime.NEUTRAL:
        return neutral_size_multiplier, neutral_extra_score
    return 0.0, 100.0  # BEAR / HIGH_VOLATILITY: yeni işlem yok
