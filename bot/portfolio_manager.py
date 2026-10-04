"""Portföy okuma, değerleme ve mevcut varlıklar için HOLD / PARTIAL_SELL / SELL kararları.

- Bot başladığında Spot hesabındaki tüm varlıklar okunur ve USDT karşılığı hesaplanır.
- Mevcut varlıklar bot başladı diye SATILMAZ. Kararlar varsayılan olarak yalnızca
  önerilir ve kaydedilir; uygulama için MANAGE_EXISTING_HOLDINGS=true ve aynı kararın
  HOLDING_CONFIRM_CYCLES tarama boyunca tekrar etmesi gerekir.
- Rotasyon yalnızca puan farkı ROTATION_MIN_SCORE_DIFF ve üzerindeyse önerilir; küçük
  puan farklarında gereksiz komisyon üretilmez.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from binance_client.client import BinanceAPIError
from bot.scanner import is_stablecoin
from bot.strategy import ScoreResult, momentum_negative, trend_broken

logger = logging.getLogger(__name__)


@dataclass
class AssetValue:
    asset: str
    free: float
    locked: float
    price_usdt: float
    value_usdt: float


@dataclass
class PortfolioSnapshot:
    equity_usdt: float
    usdt_free: float
    usdt_total: float
    assets: list[AssetValue] = field(default_factory=list)
    source: str = "paper"
    quote_asset: str = "USDT"
    fx: float = 1.0  # 1 USDT kaç birim işlem para birimi (ör. USDTTRY)
    quote_free: float | None = None

    def __post_init__(self) -> None:
        if self.quote_free is None:
            self.quote_free = self.usdt_free

    @property
    def equity_quote(self) -> float:
        """Özsermayenin işlem para birimi (USDT veya TRY) karşılığı."""
        return self.equity_usdt * self.fx

    def to_dict(self) -> dict:
        return {
            "equity_usdt": round(self.equity_usdt, 4),
            "usdt_free": round(self.usdt_free, 4),
            "usdt_total": round(self.usdt_total, 4),
            "quote_asset": self.quote_asset,
            "quote_free": round(self.quote_free or 0.0, 4),
            "equity_quote": round(self.equity_quote, 4),
            "source": self.source,
            "assets": [asdict(a) for a in self.assets],
        }


@dataclass
class HoldingDecision:
    asset: str
    symbol: str | None
    quantity: float
    value_usdt: float
    score: float | None
    decision: str  # HOLD / PARTIAL_SELL / SELL
    reason: str
    confirmations: int = 0
    executable: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def asset_price_usdt(asset: str, prices: dict[str, float]) -> float:
    if asset == "USDT":
        return 1.0
    if f"{asset}USDT" in prices:
        return prices[f"{asset}USDT"]
    if f"USDT{asset}" in prices and prices[f"USDT{asset}"] > 0:
        return 1 / prices[f"USDT{asset}"]
    if f"{asset}BTC" in prices and "BTCUSDT" in prices:
        return prices[f"{asset}BTC"] * prices["BTCUSDT"]
    return 0.0


def value_balances(balances: dict[str, dict[str, float]], prices: dict[str, float],
                   quote: str = "USDT", fx: float = 1.0) -> PortfolioSnapshot:
    assets: list[AssetValue] = []
    equity = 0.0
    usdt_free = usdt_total = 0.0
    for asset, bal in balances.items():
        # Binance Simple Earn "LD" önekli varlıklar spot işlemde kullanılamaz
        if asset.startswith("LD") and len(asset) > 3:
            continue
        total = bal.get("free", 0.0) + bal.get("locked", 0.0)
        price = asset_price_usdt(asset, prices)
        value = total * price
        equity += value
        if asset == "USDT":
            usdt_free, usdt_total = bal.get("free", 0.0), total
        assets.append(AssetValue(asset, bal.get("free", 0.0), bal.get("locked", 0.0), price, value))
    assets.sort(key=lambda a: a.value_usdt, reverse=True)
    quote_free = balances.get(quote, {}).get("free", 0.0)
    return PortfolioSnapshot(equity, usdt_free, usdt_total, assets, source="binance",
                             quote_asset=quote, fx=fx, quote_free=quote_free)


def decide_holding(score: ScoreResult | None, best_score: float | None,
                   rotation_min_diff: float, rotation_enabled: bool) -> tuple[str, str]:
    """Tek varlık için HOLD / PARTIAL_SELL / SELL kararı ve gerekçesi."""
    if score is None or score.indicators is None:
        return "HOLD", "Analiz verisi yok; varsayılan olarak tutuluyor"
    ind = score.indicators
    broken, negative = trend_broken(ind), momentum_negative(ind)
    if broken and negative:
        return "SELL", "Trend bozuldu ve momentum ciddi negatif"
    if (rotation_enabled and best_score is not None and score.score < 60
            and best_score - score.score >= rotation_min_diff):
        return "SELL", (f"Rotasyon: daha iyi fırsat var (puan farkı "
                        f"{best_score - score.score:.1f} ≥ {rotation_min_diff:.0f})")
    if broken or negative or score.score < 40:
        why = "trend bozuldu" if broken else ("momentum negatif" if negative else "puan düşük")
        return "PARTIAL_SELL", f"Zayıflama: {why} (puan {score.score:.1f})"
    return "HOLD", f"Görünüm kabul edilebilir (puan {score.score:.1f})"


class PortfolioManager:
    def __init__(self, client, db, settings, order_manager):
        self.client = client
        self.db = db
        self.s = settings
        self.om = order_manager
        self._confirm: dict[str, tuple[str, int]] = {}
        self.last_account_error: str | None = None
        self.quote = settings.quote_asset
        self.fx = 1.0  # motor her döngüde günceller

    def real_snapshot(self, prices: dict[str, float]) -> PortfolioSnapshot | None:
        if not self.client.has_keys:
            return None
        try:
            snap = value_balances(self.client.balances(), prices, self.quote, self.fx)
        except BinanceAPIError as exc:
            logger.warning("Hesap bakiyesi okunamadı: %s", exc)
            self.last_account_error = str(exc)
            return None
        self.last_account_error = None
        return snap

    @staticmethod
    def snapshot_for(client, prices: dict[str, float]) -> tuple[PortfolioSnapshot | None, str | None]:
        """Herhangi bir borsa hesabının değerlemesi: (anlık görüntü, hata)."""
        try:
            return value_balances(client.balances(), prices), None
        except BinanceAPIError as exc:
            return None, str(exc)

    def snapshot(self, prices: dict[str, float], open_trades) -> PortfolioSnapshot:
        """Bot özsermayesi: LIVE'da gerçek hesap, DRY_RUN'da kağıt hesap."""
        if self.s.is_live:
            snap = self.real_snapshot(prices)
            if snap is None:
                raise BinanceAPIError("LIVE modda hesap bakiyesi okunamadı")
            return snap
        fx = self.fx or 1.0
        cash = self.om.paper_balance  # işlem para birimi cinsinden
        assets = [AssetValue(self.quote, cash, 0.0, 1 / fx, cash / fx)]
        equity_quote = cash
        for t in open_trades:
            price = prices.get(t.symbol, t.entry_price)
            value = t.quantity * price
            equity_quote += value
            assets.append(AssetValue(t.base_asset, t.quantity, 0.0, price / fx, value / fx))
        usdt_cash = cash if self.quote == "USDT" else 0.0
        return PortfolioSnapshot(equity_quote / fx, usdt_cash, usdt_cash, assets, source="paper",
                                 quote_asset=self.quote, fx=fx, quote_free=cash)

    def assess_holdings(
        self,
        snapshot: PortfolioSnapshot,
        score_fn,
        best_score: float | None,
        bot_assets: set[str],
        tradable: set[str],
    ) -> list[HoldingDecision]:
        """Hesaptaki bot dışı varlıklar için karar üretir. score_fn(symbol) -> ScoreResult|None."""
        decisions: list[HoldingDecision] = []
        seen: set[str] = set()
        for a in snapshot.assets:
            if a.asset in ("USDT", self.quote) or is_stablecoin(a.asset) or a.asset in bot_assets:
                continue
            if a.value_usdt < self.s.min_holding_value_usdt:
                continue
            seen.add(a.asset)
            symbol = f"{a.asset}{self.quote}"
            qty = a.free + a.locked
            if symbol not in tradable:
                decisions.append(HoldingDecision(a.asset, None, qty, a.value_usdt, None, "HOLD",
                                                 f"İşleme uygun {self.quote} paritesi yok"))
                continue
            try:
                score = score_fn(symbol)
            except BinanceAPIError as exc:
                score = None
                logger.warning("%s puanlanamadı: %s", symbol, exc)
            decision, reason = decide_holding(score, best_score, self.s.rotation_min_score_diff,
                                              self.s.rotation_enabled)
            prev = self._confirm.get(a.asset)
            count = prev[1] + 1 if prev and prev[0] == decision else 1
            self._confirm[a.asset] = (decision, count)
            executable = (
                decision != "HOLD"
                and self.s.manage_existing_holdings
                and self.s.is_live
                and count >= self.s.holding_confirm_cycles
            )
            decisions.append(HoldingDecision(a.asset, symbol, qty, a.value_usdt,
                                             score.score if score else None, decision, reason,
                                             count, executable))
        for gone in set(self._confirm) - seen:
            self._confirm.pop(gone, None)
        return decisions
