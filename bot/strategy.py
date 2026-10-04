"""Teknik göstergeler, fırsat puanı (0-100), çıkış sinyalleri ve beklenen net avantaj.

Bu modül saf hesaplama içerir; ağ çağrısı yapmaz, bu yüzden kolayca test edilir.
Puanlama bir olasılık modeli değil, kural tabanlı bir sezgiseldir. Kâr garanti etmez.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# --- Göstergeler -----------------------------------------------------------


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> list[float]:
    """Wilder RSI. İlk `period` değer 50 kabul edilir."""
    n = len(closes)
    if n < period + 1:
        return [50.0] * n
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [50.0] * (period + 1)

    def _val(g: float, l: float) -> float:
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[-1] = _val(avg_gain, avg_loss)
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(_val(avg_gain, avg_loss))
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    fast_e, slow_e = ema(closes, fast), ema(closes, slow)
    line = [f - s for f, s in zip(fast_e, slow_e)]
    sig = ema(line, signal)
    hist = [l - s for l, s in zip(line, sig)]
    return line, sig, hist


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float]:
    n = len(closes)
    if n == 0:
        return []
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if n < period:
        avg = sum(trs) / n
        return [avg] * n
    out = [sum(trs[:period]) / period] * period
    for i in range(period, n):
        out.append((out[-1] * (period - 1) + trs[i]) / period)
    return out


def stdev_returns(closes: list[float], window: int = 24) -> float:
    if len(closes) < window + 1:
        return 0.0
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - window, len(closes))]
    mean = sum(rets) / len(rets)
    return math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))


@dataclass
class Candles:
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    quote_volumes: list[float]

    @classmethod
    def from_klines(cls, klines: list[list]) -> "Candles":
        return cls(
            opens=[float(k[1]) for k in klines],
            highs=[float(k[2]) for k in klines],
            lows=[float(k[3]) for k in klines],
            closes=[float(k[4]) for k in klines],
            quote_volumes=[float(k[7]) for k in klines],
        )

    def __len__(self) -> int:
        return len(self.closes)


@dataclass
class Indicators:
    close: float
    ema20: float
    ema50: float
    ema200: float
    rsi: float
    macd: float
    macd_signal: float
    macd_hist: float
    macd_hist_prev: float
    atr: float
    atr_pct: float
    volatility: float
    volume_ratio: float
    last_candle_change_pct: float
    ema50_slope_pct: float


def compute_indicators(c: Candles, atr_period: int = 14) -> Indicators:
    closes = c.closes
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    r = rsi(closes, 14)
    line, sig, hist = macd(closes)
    a = atr(c.highs, c.lows, closes, atr_period)
    recent = c.quote_volumes[-24:]
    prior = c.quote_volumes[-96:-24] or c.quote_volumes[:-24] or recent
    recent_avg = sum(recent) / max(len(recent), 1)
    prior_avg = sum(prior) / max(len(prior), 1)
    last_open = c.opens[-1] if c.opens[-1] else closes[-1]
    slope_base = e50[-11] if len(e50) > 11 else e50[0]
    return Indicators(
        close=closes[-1],
        ema20=e20[-1],
        ema50=e50[-1],
        ema200=e200[-1],
        rsi=r[-1],
        macd=line[-1],
        macd_signal=sig[-1],
        macd_hist=hist[-1],
        macd_hist_prev=hist[-2] if len(hist) > 1 else hist[-1],
        atr=a[-1],
        atr_pct=a[-1] / closes[-1] if closes[-1] else 0.0,
        volatility=stdev_returns(closes, 24),
        volume_ratio=recent_avg / prior_avg if prior_avg > 0 else 1.0,
        last_candle_change_pct=(closes[-1] / last_open - 1) * 100,
        ema50_slope_pct=(e50[-1] / slope_base - 1) * 100 if slope_base else 0.0,
    )


# --- Puanlama --------------------------------------------------------------


@dataclass
class ScoreResult:
    symbol: str
    score: float
    price: float
    atr: float
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    eligible: bool = False
    indicators: Indicators | None = None


def score_symbol(
    symbol: str,
    candles: Candles,
    quote_volume_24h: float,
    spread_pct: float,
    regime: str,
    max_spread_pct: float = 0.0015,
    max_extension_atr: float = 2.5,
    max_last_candle_change_pct: float = 5.0,
    min_volume: float = 20_000_000,
) -> ScoreResult:
    """0-100 arası fırsat puanı ve giriş için sert kuralları değerlendirir.

    Hacim puanı piyasanın minimum hacim eşiğine (min_volume, USDT) göre ölçeklenir.
    """
    ind = compute_indicators(candles)
    comp: dict[str, float] = {}
    reasons: list[str] = []

    # EMA dizilimi (25)
    if ind.close > ind.ema20 > ind.ema50 > ind.ema200:
        comp["ema_trend"] = 25
    elif ind.close > ind.ema50 > ind.ema200:
        comp["ema_trend"] = 17
    elif ind.close > ind.ema200:
        comp["ema_trend"] = 8
    else:
        comp["ema_trend"] = 0
        reasons.append("Fiyat EMA200 altında")

    # Trend gücü (10): EMA20-EMA50 farkının ATR'ye oranı ve EMA50 eğimi
    strength = (ind.ema20 - ind.ema50) / ind.atr if ind.atr > 0 else 0.0
    comp["trend_strength"] = round(max(0.0, min(strength, 2.0)) / 2.0 * 7 + (3 if ind.ema50_slope_pct > 0 else 0), 2)

    # RSI (15)
    if 50 <= ind.rsi <= 65:
        comp["rsi"] = 15
    elif 45 <= ind.rsi < 50 or 65 < ind.rsi <= 70:
        comp["rsi"] = 8
    else:
        comp["rsi"] = 0
        reasons.append(f"RSI uygun aralıkta değil ({ind.rsi:.1f})")

    # MACD (15)
    if ind.macd_hist > 0 and ind.macd_hist >= ind.macd_hist_prev:
        comp["macd"] = 15
    elif ind.macd_hist > 0:
        comp["macd"] = 10
    elif ind.macd_hist > ind.macd_hist_prev:
        comp["macd"] = 4
    else:
        comp["macd"] = 0
        reasons.append("MACD negatif ve zayıflıyor")

    # ATR (6) ve volatilite (4)
    if 0.005 <= ind.atr_pct <= 0.03:
        comp["atr"] = 6
    elif 0.003 <= ind.atr_pct <= 0.05:
        comp["atr"] = 3
    else:
        comp["atr"] = 0
        reasons.append(f"ATR% uygun değil ({ind.atr_pct * 100:.2f}%)")
    if ind.volatility <= 0.02:
        comp["volatility"] = 4
    elif ind.volatility <= 0.035:
        comp["volatility"] = 2
    else:
        comp["volatility"] = 0
        reasons.append("Volatilite yüksek")

    # 24s hacim (10)
    if quote_volume_24h >= 10 * min_volume:
        comp["volume_24h"] = 10
    elif quote_volume_24h >= 2.5 * min_volume:
        comp["volume_24h"] = 7
    elif quote_volume_24h >= min_volume:
        comp["volume_24h"] = 4
    else:
        comp["volume_24h"] = 0

    # Hacim artışı (5) - aşırı artış pump işareti sayılır
    if 1.1 <= ind.volume_ratio <= 3.0:
        comp["volume_growth"] = 5
    elif 0.8 <= ind.volume_ratio < 1.1:
        comp["volume_growth"] = 2
    elif ind.volume_ratio > 3.0:
        comp["volume_growth"] = 1
        reasons.append("Anormal hacim artışı (pump riski)")
    else:
        comp["volume_growth"] = 0

    # Spread (5)
    if spread_pct <= 0.0003:
        comp["spread"] = 5
    elif spread_pct <= 0.0008:
        comp["spread"] = 3
    elif spread_pct <= max_spread_pct:
        comp["spread"] = 1
    else:
        comp["spread"] = 0

    # BTC piyasa yönü (5)
    comp["btc_regime"] = {"BULL": 5, "NEUTRAL": 3}.get(regime, 0)

    score = round(min(100.0, sum(comp.values())), 2)

    # Sert giriş kuralları (puan yüksek olsa bile)
    eligible = True
    if len(candles) < 200:
        eligible = False
        reasons.append("Fiyat geçmişi yetersiz")
    if ind.close > ind.ema20 + max_extension_atr * ind.atr:
        eligible = False
        reasons.append("Fiyat EMA20'den çok uzak (pump kovalanmaz)")
    if ind.rsi > 72:
        eligible = False
        reasons.append("RSI aşırı alım bölgesinde")
    if abs(ind.last_candle_change_pct) > max_last_candle_change_pct:
        eligible = False
        reasons.append("Son mumda aşırı hareket")
    if spread_pct > max_spread_pct:
        eligible = False
        reasons.append("Spread yüksek")
    if ind.atr <= 0:
        eligible = False
        reasons.append("ATR hesaplanamadı")

    return ScoreResult(symbol=symbol, score=score, price=ind.close, atr=ind.atr,
                       components=comp, reasons=reasons, eligible=eligible, indicators=ind)


# --- Çıkış sinyalleri ------------------------------------------------------


def trend_broken(ind: Indicators) -> bool:
    """Kapanış EMA50 altında ve EMA20 EMA50'nin altına inmiş."""
    return ind.close < ind.ema50 and ind.ema20 < ind.ema50


def momentum_negative(ind: Indicators) -> bool:
    """MACD histogramı negatif ve düşüyor, RSI 40 altında."""
    return ind.macd_hist < 0 and ind.macd_hist < ind.macd_hist_prev and ind.rsi < 40


# --- Beklenen net avantaj --------------------------------------------------


def win_probability(score: float) -> float:
    """Puandan muhafazakâr kazanma olasılığı tahmini (0.30 - 0.60)."""
    return max(0.30, min(0.60, 0.30 + (score - 50) / 50 * 0.30))


@dataclass
class EdgeResult:
    expected_net_pct: float
    gross_target_pct: float
    stop_pct: float
    cost_pct: float
    ok: bool
    reason: str


def expected_edge(
    score: float,
    entry: float,
    stop: float,
    reward_risk: float,
    round_trip_cost_pct: float,
    min_edge_pct: float,
    min_cost_coverage: float,
) -> EdgeResult:
    """Komisyon ve slippage sonrası beklenen net getiri (oran).

    EV = p * hedef% - (1-p) * stop% - maliyet%
    """
    if entry <= 0 or stop <= 0 or stop >= entry:
        return EdgeResult(0, 0, 0, round_trip_cost_pct, False, "Geçersiz stop")
    stop_pct = (entry - stop) / entry
    target_pct = stop_pct * reward_risk
    p = win_probability(score)
    ev = p * target_pct - (1 - p) * stop_pct - round_trip_cost_pct
    if target_pct < round_trip_cost_pct * min_cost_coverage:
        return EdgeResult(ev, target_pct, stop_pct, round_trip_cost_pct, False,
                          "Hedef hareket komisyon+slippage maliyetini yeterince karşılamıyor")
    if ev < min_edge_pct:
        return EdgeResult(ev, target_pct, stop_pct, round_trip_cost_pct, False,
                          f"Beklenen net avantaj yetersiz ({ev * 100:.2f}%)")
    return EdgeResult(ev, target_pct, stop_pct, round_trip_cost_pct, True, "OK")
