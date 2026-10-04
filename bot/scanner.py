"""Binance Spot USDT paritelerini tarar, uygun olmayanları eler ve puanlar.

Sabit coin listesi yoktur. Eleme kriterleri:
- işlem durumu TRADING olmayan / spot işleme kapalı semboller (delist süreci)
- kullanıcı kara listesi (SYMBOL_BLACKLIST) - delist uyarısı alan varlıklar için
- stablecoin/stablecoin çiftleri, leveraged tokenlar (UP/DOWN/BULL/BEAR)
- düşük 24s hacim, yüksek spread (düşük likidite)
- aşırı pump/dump (24s değişim), son mumda aşırı hareket
- yeni listelenmiş coinler ve çok kısa fiyat geçmişi
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from binance_client.client import BinanceAPIError
from bot.strategy import Candles, ScoreResult, score_symbol

logger = logging.getLogger(__name__)

STABLECOINS = {
    "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "PAX", "UST", "USTC", "USDD",
    "PYUSD", "EUR", "EURI", "AEUR", "GBP", "TRY", "BRL", "ARS", "BIDR", "IDRT", "UAH",
    "RUB", "ZAR", "NGN", "PLN", "RON", "JPY", "MXN", "COP", "CZK", "XUSD", "USD1", "BFUSD",
    "RLUSD", "USDE", "PAXG", "XAUT",
}
LEVERAGED_PATTERN = re.compile(r".+(UP|DOWN|BULL|BEAR)$")


def is_stablecoin(asset: str) -> bool:
    return asset.upper() in STABLECOINS


def is_leveraged_token(asset: str) -> bool:
    a = asset.upper()
    return bool(LEVERAGED_PATTERN.fullmatch(a)) and a not in {"JUP", "SUP"}


@dataclass
class ScanResult:
    scan_id: str
    scored: list[ScoreResult] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    universe_size: int = 0
    market: dict[str, tuple[float, float]] = field(default_factory=dict)  # sembol -> (hacim, spread)

    @property
    def best(self) -> ScoreResult | None:
        eligible = [s for s in self.scored if s.eligible]
        return max(eligible, key=lambda s: s.score) if eligible else None


class MarketScanner:
    LISTING_CACHE_TTL = 24 * 3600

    def __init__(self, client, settings):
        self.client = client
        self.s = settings
        self._listing_cache: dict[str, tuple[float, int]] = {}

    def tradable_symbols(self) -> dict[str, dict]:
        """Durum/stable/leveraged/kara liste filtrelerinden geçen USDT spot sembolleri."""
        out: dict[str, dict] = {}
        blacklist = self.s.blacklist
        for info in self.client.symbols_raw():
            sym = info["symbol"]
            base = info.get("baseAsset", "")
            if info.get("quoteAsset") != self.s.quote_asset:
                continue
            if info.get("status") != "TRADING" or not info.get("isSpotTradingAllowed", True):
                continue
            if sym in blacklist or base in blacklist:
                continue
            if is_stablecoin(base) or is_leveraged_token(base):
                continue
            if "MARKET" not in info.get("orderTypes", ["MARKET"]):
                continue
            out[sym] = info
        return out

    @staticmethod
    def market_data(tickers: list[dict], books: dict[str, dict]) -> dict[str, tuple[float, float]]:
        out: dict[str, tuple[float, float]] = {}
        for t in tickers:
            sym = t.get("symbol")
            book = books.get(sym)
            if not book:
                continue
            bid, ask = float(book.get("bidPrice", 0)), float(book.get("askPrice", 0))
            if bid > 0 and ask > 0:
                out[sym] = (float(t.get("quoteVolume", 0) or 0), (ask - bid) / ((ask + bid) / 2))
        return out

    def prefilter(self, tickers: list[dict], books: dict[str, dict],
                  universe: dict[str, dict]) -> tuple[list[tuple[str, float, float]], dict[str, str]]:
        """Hacim, spread ve 24s değişim ön elemesi. (sembol, hacim, spread) listesi döner."""
        rejected: dict[str, str] = {}
        passed: list[tuple[str, float, float]] = []
        for t in tickers:
            sym = t.get("symbol")
            if sym not in universe:
                continue
            qv = float(t.get("quoteVolume", 0) or 0)
            change = float(t.get("priceChangePercent", 0) or 0)
            if qv < self.s.min_quote_volume_usdt:
                rejected[sym] = "Düşük hacim"
                continue
            if abs(change) > self.s.max_abs_24h_change_pct:
                rejected[sym] = f"Aşırı pump/dump ({change:.1f}%)"
                continue
            book = books.get(sym)
            if not book:
                rejected[sym] = "Emir defteri verisi yok"
                continue
            bid, ask = float(book.get("bidPrice", 0)), float(book.get("askPrice", 0))
            if bid <= 0 or ask <= 0:
                rejected[sym] = "Düşük likidite (boş defter)"
                continue
            spread = (ask - bid) / ((ask + bid) / 2)
            if spread > self.s.max_spread_pct:
                rejected[sym] = f"Yüksek spread ({spread * 100:.3f}%)"
                continue
            passed.append((sym, qv, spread))
        passed.sort(key=lambda x: x[1], reverse=True)
        return passed[: self.s.max_candidates], rejected

    def listing_days(self, symbol: str) -> int:
        cached = self._listing_cache.get(symbol)
        if cached and time.time() - cached[0] < self.LISTING_CACHE_TTL:
            return cached[1]
        daily = self.client.klines(symbol, "1d", limit=self.s.min_listing_days + 5)
        days = len(daily)
        self._listing_cache[symbol] = (time.time(), days)
        return days

    def score_one(self, symbol: str, quote_volume: float, spread: float, regime: str) -> ScoreResult:
        klines = self.client.klines(symbol, self.s.kline_interval, limit=self.s.kline_limit)
        candles = Candles.from_klines(klines)
        return score_symbol(
            symbol, candles, quote_volume, spread, regime,
            max_spread_pct=self.s.max_spread_pct,
            max_extension_atr=self.s.max_extension_atr,
            max_last_candle_change_pct=self.s.max_last_candle_change_pct,
        )

    def scan(self, regime: str, tickers: list[dict] | None = None,
             books: dict[str, dict] | None = None) -> ScanResult:
        result = ScanResult(scan_id=str(int(time.time() * 1000)))
        universe = self.tradable_symbols()
        result.universe_size = len(universe)
        tickers = tickers if tickers is not None else self.client.ticker_24h_all()
        books = books if books is not None else self.client.book_tickers()
        result.market = self.market_data(tickers, books)
        candidates, rejected = self.prefilter(tickers, books, universe)
        result.rejected.update(rejected)
        for sym, qv, spread in candidates:
            try:
                if self.listing_days(sym) < self.s.min_listing_days:
                    result.rejected[sym] = "Yeni listelenmiş"
                    continue
                res = self.score_one(sym, qv, spread, regime)
            except BinanceAPIError as exc:
                result.rejected[sym] = f"Veri alınamadı: {exc}"
                continue
            if not res.eligible and "Fiyat geçmişi yetersiz" in res.reasons:
                result.rejected[sym] = "Çok kısa fiyat geçmişi"
            result.scored.append(res)
        result.scored.sort(key=lambda s: s.score, reverse=True)
        return result
