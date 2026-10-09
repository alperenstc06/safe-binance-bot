"""Bot motoru: tarama, giriş, pozisyon yönetimi, rotasyon ve güvenlik kontrolleri."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from binance_client.client import BinanceAPIError
from bot.market_regime import Regime, RegimeResult, detect_regime, regime_adjustments
from bot.order_manager import FillResult, OrderError, OrderManager
from bot.portfolio_manager import HoldingDecision, PortfolioManager, PortfolioSnapshot
from bot.risk_manager import (
    RiskManager,
    compute_stop_update,
    initial_stop_price,
    stop_hit,
    utc_day_start,
    wick_aware_stop,
)
from bot.scanner import MarketScanner, ScanResult
from bot.strategy import (
    Candles,
    ScoreResult,
    compute_indicators,
    expected_edge,
    momentum_negative,
    trend_broken,
)
from database.database import PARTIAL_TP_REASON, Database
from database.models import Signal, Trade, utcnow

logger = logging.getLogger(__name__)

EMERGENCY_KEY = "emergency_stop"
RUNNING_KEY = "running"
REVIEW_KEY = "review_required"
# Komisyon/yuvarlama kaynaklı kabul edilebilir miktar farkı (oran)
QTY_TOLERANCE = 0.02
# Borsa stopu bakiye doğrulanamadığı için bu kadar döngü konamazsa inceleme istenir
MAX_UNVERIFIED_STOP_CYCLES = 10
PARTIAL_PREFIX = "PARTIAL"


class ReviewRequired(OrderError):
    """Kayıt ile borsa durumu tutarsız: otomatik işlem yapılmaz, kullanıcı incelemesi gerekir."""


class SafetyError(RuntimeError):
    pass


class BotEngine:
    def __init__(self, settings, client, db: Database, accounts: dict | None = None):
        self.s = settings
        self.client = client
        self.db = db
        self.mode = settings.trading_mode.value
        self.exchange = settings.trading_exchange.value
        # Kayıt defteri: Global için "DRY_RUN"/"LIVE", TR için "DRY_RUN@BINANCE_TR" vb.
        self.book = self.mode if self.exchange == "BINANCE_GLOBAL" else f"{self.mode}@{self.exchange}"
        self.quote = settings.quote_asset
        self.fx = 1.0  # 1 USDT = fx birim işlem para birimi
        self.accounts = dict(accounts or {})
        self.accounts[self.exchange] = client
        self.account_views: dict[str, dict] = {}
        self.risk = RiskManager(settings, db, self.book)
        self.orders = OrderManager(client, db, settings, book=self.book)
        self.portfolio = PortfolioManager(client, db, settings, self.orders)
        self.scanner = MarketScanner(client, settings)

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self.regime: RegimeResult | None = None
        self.last_scan: ScanResult | None = None
        self.last_scan_at: float = 0.0
        self.last_cycle_at: datetime | None = None
        self.last_error: str | None = None
        self.no_trade_reason: str = "Bot henüz bir tarama yapmadı"
        self.holding_decisions: list[HoldingDecision] = []
        self.snapshot: PortfolioSnapshot | None = None
        self.real_snapshot: PortfolioSnapshot | None = None
        self.prices: dict[str, float] = {}
        self._synced = False
        self._stop_errors: dict[str, str] = {}
        self._last_stop_failure: str | None = None  # "BALANCE" / "ORDER" / None
        self._unverified_cycles: dict[int, int] = {}

    # ------------------------------------------------------------------ durum
    @property
    def emergency(self) -> bool:
        return bool(self.db.get_state(EMERGENCY_KEY, False))

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -------------------------------------------------------------- kontroller
    def safety_check(self) -> None:
        """LIVE başlatma öncesi API anahtarı yetki kontrolü."""
        if not self.s.is_live:
            return
        if not self.client.has_keys:
            raise SafetyError("LIVE mod için API anahtarı gerekli")
        try:
            perms = self.client.api_permissions()
        except BinanceAPIError as exc:
            raise SafetyError(f"API anahtarı yetkileri okunamadı: {exc}") from exc
        if perms.get("enableWithdrawals"):
            raise SafetyError("API anahtarında para çekme (withdraw) yetkisi açık. Güvenlik için "
                              "bu yetkiyi kapatın; bot bu anahtarla çalışmaz.")
        if not perms.get("enableSpotAndMarginTrading", True):
            raise SafetyError("API anahtarında Spot işlem yetkisi kapalı")
        if perms.get("permissions_verifiable") is False:
            self.db.log_decision("SAFETY", f"{self.exchange} API anahtar yetkileri otomatik doğrulanamıyor. "
                                           f"Para çekme (withdraw) yetkisinin KAPALI olduğunu borsa "
                                           f"panelinden kontrol edin.", level="WARNING")
        for flag, name in (("enableFutures", "Futures"), ("enableMargin", "Margin")):
            if perms.get(flag):
                self.db.log_decision("SAFETY", f"Uyarı: API anahtarında {name} yetkisi açık; bot "
                                               f"bunu kullanmaz ama kapatmanız önerilir.", level="WARNING")

    def start(self) -> None:
        with self._lock:
            if self.emergency:
                raise SafetyError("Acil durdurma aktif. Önce acil durumu sıfırlayın.")
            if self.running:
                return
            self.safety_check()
            self.sync_positions()
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, name="bot-engine", daemon=True)
            self._thread.start()
            self.db.set_state(RUNNING_KEY, True)
            self.db.log_decision("BOT", f"Bot başlatıldı ({self.mode}, {self.exchange})")

    def stop(self, reason: str = "Kullanıcı tarafından durduruldu") -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self.s.binance_timeout_seconds * 3 + 5)
        self._thread = None
        self.db.set_state(RUNNING_KEY, False)
        self.db.log_decision("BOT", f"Bot durduruldu: {reason}")

    def emergency_stop(self) -> None:
        """Yeni işlemleri anında engeller ve döngüyü durdurur.

        Açık pozisyonlar ve borsa üzerindeki koruyucu stop emirleri korunur (stopsuz
        pozisyon bırakmamak için). Pozisyonları kapatmak için ayrıca
        'Tüm Pozisyonları Kapat' kullanılır.
        """
        self.db.set_state(EMERGENCY_KEY, True)
        self.no_trade_reason = "Acil durdurma aktif"
        self.db.log_decision("EMERGENCY", "ACİL DURDURMA etkinleştirildi", level="WARNING")
        self.stop("Acil durdurma")

    def reset_emergency(self) -> None:
        self.db.set_state(EMERGENCY_KEY, False)
        self.db.log_decision("EMERGENCY", "Acil durdurma sıfırlandı", level="WARNING")

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_cycle()
            except Exception as exc:  # döngü asla çökmemeli
                self.last_error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, BinanceAPIError):
                    logger.error("Binance bağlantı hatası: %s", exc)
                else:
                    logger.exception("Döngü hatası")
                try:
                    self.db.log_decision("ERROR", self.last_error, level="ERROR")
                except Exception:
                    logger.exception("Hata kaydedilemedi")
            self._stop_event.wait(self.s.loop_interval_seconds)

    # -------------------------------------------------------- senkronizasyon
    def sync_positions(self) -> list[str]:
        """Yeniden başlatma sonrası açık pozisyonları DB ve borsa ile eşitler."""
        messages: list[str] = []
        with self._lock:
            open_trades = self.db.open_trades(self.book)
            if not open_trades:
                self._synced = True
                return messages
            self.client.load_exchange_info()
            balances = self.client.balances() if self.s.is_live else {}
            for t in open_trades:
                filters = self.client.get_filters(t.symbol)
                if not self.s.is_live:
                    messages.append(f"{t.symbol}: DRY_RUN pozisyonu geri yüklendi")
                    continue
                status, fill = self.orders.check_stop_filled(t.symbol, t.stop_order_id)
                if status in ("FILLED", "GONE") and fill is not None:
                    t.stop_order_id = None
                    try:
                        closed = self._apply_fill(t, fill, "STOP_LOSS_EXCHANGE", filters)
                    except ReviewRequired:
                        messages.append(f"{t.symbol}: kayıt tutarsız, inceleme gerekiyor")
                        continue
                    if closed:
                        messages.append(f"{t.symbol}: kapalıyken borsa stopu tetiklenmiş, kapatıldı")
                        continue
                    status = "GONE"
                bal = balances.get(t.base_asset, {"free": 0.0, "locked": 0.0})
                held = bal["free"] + bal["locked"]
                price = self.client.price(t.symbol)
                if held < t.quantity:
                    if t.quantity * t.entry_price < 0.5 * t.entry_quote:
                        self.flag_review(f"{t.symbol} (#{t.id}): kayıtlı miktar maliyetle tutarsız")
                        messages.append(f"{t.symbol}: kayıt tutarsız, inceleme gerekiyor")
                        continue
                    if held <= 0 or held * price < float(filters.min_notional or 0):
                        est = FillResult(price, t.quantity, t.quantity * price * (1 - self.orders.fee_rate),
                                         t.quantity * price * self.orders.fee_rate)
                        if t.stop_order_id:
                            self.orders.cancel_stop(t.symbol, t.stop_order_id, t.base_asset)
                        self._finalize_close(t, est, "EXTERNAL_CLOSE",
                                             note="Varlık hesapta bulunamadı; harici olarak kapatılmış "
                                                  "(PNL piyasa fiyatıyla tahmini)")
                        messages.append(f"{t.symbol}: varlık hesapta yok, harici kapanış olarak işlendi "
                                        f"(PNL tahmini)")
                        continue
                    if held >= t.quantity * (1 - QTY_TOLERANCE):
                        # komisyon/yuvarlama farkı: miktar düzeltilir, maliyet aynı kalır
                        t.quantity = float(filters.round_qty(held))
                        messages.append(f"{t.symbol}: miktar hesap bakiyesine göre güncellendi ({t.quantity})")
                    else:
                        # kalıcı eksik: dışarıda satılan kısım tahmini fiyatla ayrı kaydedilir,
                        # kalan miktar ve oransal maliyet açık kalır
                        gone = t.quantity - held
                        est = FillResult(price, gone, gone * price * (1 - self.orders.fee_rate),
                                         gone * price * self.orders.fee_rate)
                        self._record_partial(t, est, "EXTERNAL")
                        t.quantity = float(filters.round_qty(held))
                        messages.append(f"{t.symbol}: {gone:.8g} adet hesapta yok (harici satış?); "
                                        f"kalan {t.quantity:.8g} açık, satılan kısım PNL'i tahmini")
                if status in ("NONE", "GONE"):
                    t.stop_order_id, t.stop_order_price = self._safe_place_stop(t, filters)
                    messages.append(f"{t.symbol}: koruyucu stop emri yeniden yerleştirildi" if t.stop_order_id
                                    else f"{t.symbol}: borsa stop emri KONAMADI (yazılım stopu aktif, "
                                         f"her döngüde tekrar denenecek)")
                self.db.save_trade(t)
            for m in messages:
                self.db.log_decision("SYNC", m)
            self._synced = True
        return messages

    # ----------------------------------------------------------------- döngü
    def run_cycle(self, force_scan: bool = False) -> None:
        with self._lock:
            now = utcnow()
            if not self._synced:
                self.sync_positions()
            filters = self.client.load_exchange_info()
            if self.last_cycle_at is None:
                self.orders.refresh_fee_rate()
            self.prices = self.client.prices()
            self.fx = self._fx_rate()
            self.portfolio.fx = self.fx
            self.orders.ensure_paper_balance(self.fx)

            for t in self.db.open_trades(self.book):
                if t.symbol not in filters:
                    self.db.log_decision("POSITION", f"{t.symbol} artık exchangeInfo'da yok", level="WARNING")
                    continue
                try:
                    self._manage_position(t, filters[t.symbol], now)
                except (BinanceAPIError, OrderError) as exc:
                    self.db.log_decision("POSITION", f"{t.symbol} yönetim hatası: {exc}", level="ERROR")

            open_trades = self.db.open_trades(self.book)
            self.snapshot = self.portfolio.snapshot(self.prices, open_trades)
            unrealized = sum(self.unrealized_pnl(t) for t in open_trades)
            stat = self._update_daily(now, self.snapshot.equity_quote)

            due = force_scan or time.time() - self.last_scan_at >= self.s.scan_interval_seconds
            if due:
                self._scan_and_trade(now, stat.start_equity, unrealized)
                self.last_scan_at = time.time()

            open_trades = self.db.open_trades(self.book)
            self.snapshot = self.portfolio.snapshot(self.prices, open_trades)
            unrealized = sum(self.unrealized_pnl(t) for t in open_trades)
            self._update_daily(now, self.snapshot.equity_quote)
            self.db.add_pnl_snapshot(
                equity_usdt=self.snapshot.equity_usdt, usdt_balance=self.snapshot.quote_free,
                daily_pnl=self.risk.daily_pnl(now, unrealized),
                total_pnl=self.db.total_realized_pnl(self.book) + unrealized,
                unrealized_pnl=unrealized,
            )
            self.last_cycle_at = now
            self.last_error = None

    def _scan_and_trade(self, now: datetime, day_start_equity: float, unrealized: float) -> None:
        tickers = self.client.ticker_24h_all()
        books = self.client.book_tickers()
        self.regime = self._detect_regime(tickers)
        scan = self.scanner.scan(self.regime.regime.value, tickers=tickers, books=books, fx=self.fx)
        self.last_scan = scan
        self._save_signals(scan)
        tradable = set(self.scanner.tradable_symbols())
        best = scan.best

        # Mevcut (bot dışı) varlıklar için kararlar
        prev_error = self.portfolio.last_account_error
        self.real_snapshot = self.snapshot if self.s.is_live else self.portfolio.real_snapshot(self.prices)
        error = self.portfolio.last_account_error
        if error and error != prev_error:
            self.db.log_decision("ACCOUNT", f"{self.exchange} hesap bakiyesi okunamadı: {error}", level="ERROR")
        self._refresh_account_views()
        if self.real_snapshot is not None:
            bot_assets = {t.base_asset for t in self.db.open_trades(self.book)}
            self.holding_decisions = self.portfolio.assess_holdings(
                self.real_snapshot, lambda sym: self._score_symbol(sym, scan),
                best.score if best else None, bot_assets, tradable)
            self._execute_holding_decisions(self.holding_decisions)

        self._try_rotation(scan, now, day_start_equity, unrealized)
        open_trades = self.db.open_trades(self.book)
        unrealized = sum(self.unrealized_pnl(t) for t in open_trades)
        self._try_open(scan, now, day_start_equity, unrealized, len(open_trades))

    def _refresh_account_views(self) -> None:
        """Panelde gösterilecek tüm borsa hesaplarını (yalnızca okuma) günceller."""
        views: dict[str, dict] = {}
        for name, client in self.accounts.items():
            view = {"exchange": name, "trading": name == self.exchange,
                    "has_api_keys": bool(getattr(client, "has_keys", False)),
                    "error": None, "portfolio": None}
            if name == self.exchange:
                view["error"] = self.portfolio.last_account_error
                view["portfolio"] = self.real_snapshot.to_dict() if self.real_snapshot else None
            elif view["has_api_keys"]:
                snap, err = self.portfolio.snapshot_for(client, self.prices)
                prev = self.account_views.get(name, {}).get("error")
                if err and err != prev:
                    self.db.log_decision("ACCOUNT", f"{name} hesap bakiyesi okunamadı: {err}", level="ERROR")
                view["error"] = err
                view["portfolio"] = snap.to_dict() if snap else None
            views[name] = view
        self.account_views = views

    def _fx_rate(self) -> float:
        """1 USDT'nin işlem para birimindeki karşılığı (USDT için 1, TRY için USDTTRY)."""
        if self.quote == "USDT":
            return 1.0
        direct = self.prices.get(f"USDT{self.quote}")
        if direct:
            return direct
        inverse = self.prices.get(f"{self.quote}USDT")
        if inverse:
            return 1 / inverse
        raise BinanceAPIError(f"USDT/{self.quote} kuru alınamadı")

    def _detect_regime(self, tickers: list[dict]) -> RegimeResult:
        btc = next((t for t in tickers if t.get("symbol") == "BTCUSDT"), {})
        candles = Candles.from_klines(self.client.klines("BTCUSDT", self.s.regime_interval, limit=300))
        result = detect_regime(candles, float(btc.get("priceChangePercent", 0) or 0),
                               self.s.high_volatility_atr_pct, self.s.high_volatility_24h_change_pct)
        prev = self.regime.regime if self.regime else None
        if prev != result.regime:
            self.db.log_decision("REGIME", f"Piyasa rejimi: {result.regime.value} - {result.reason}")
        return result

    def _score_symbol(self, symbol: str, scan: ScanResult) -> ScoreResult:
        for s in scan.scored:
            if s.symbol == symbol:
                return s
        qv, spread = scan.market.get(symbol, (0.0, 1.0))
        regime = self.regime.regime.value if self.regime else Regime.NEUTRAL.value
        return self.scanner.score_one(symbol, qv, spread, regime)

    def _save_signals(self, scan: ScanResult) -> None:
        regime = self.regime.regime.value if self.regime else "UNKNOWN"
        self.db.save_signals([
            Signal(scan_id=scan.scan_id, symbol=s.symbol, score=s.score, price=s.price,
                   regime=regime, eligible=s.eligible, components=s.components, reasons=s.reasons)
            for s in scan.scored[:20]
        ])

    # ---------------------------------------------------------------- giriş
    def _min_score(self) -> tuple[float, float]:
        regime = self.regime.regime if self.regime else Regime.NEUTRAL
        size_mult, extra = regime_adjustments(regime, self.s.neutral_size_multiplier,
                                              self.s.neutral_extra_score)
        return self.s.min_opportunity_score + extra, size_mult

    def _set_reason(self, reason: str) -> None:
        if reason != self.no_trade_reason:
            self.db.log_decision("NO_TRADE", reason)
        self.no_trade_reason = reason

    def _try_open(self, scan: ScanResult, now: datetime, day_start_equity: float,
                  unrealized: float, open_count: int) -> None:
        if self.regime is not None and not self.regime.allows_new_trades:
            self._set_reason(f"Piyasa rejimi {self.regime.regime.value}: yeni işlem durduruldu")
            return
        flags = self.review_flags()
        if flags:
            self._set_reason(f"İnceleme gerekiyor, yeni işlem açılmıyor: {flags[0]}")
            return
        check = self.risk.can_open_trade(now, open_count, day_start_equity, unrealized, self.emergency)
        if not check.allowed:
            self._set_reason(check.reason)
            return
        min_score, size_mult = self._min_score()
        candidates = [s for s in scan.scored if s.eligible and s.score >= min_score]
        if not candidates:
            top = scan.scored[0] if scan.scored else None
            msg = (f"Uygun fırsat yok: en yüksek puan {top.symbol} {top.score:.1f} "
                   f"(gereken ≥ {min_score:.0f})" if top else
                   f"Uygun fırsat yok: aday coin bulunamadı ({scan.summary()})")
            self._set_reason(msg)
            return

        last_reason = ""
        for cand in candidates[:3]:
            ok, reason = self._open_position(cand, size_mult)
            if ok:
                self.no_trade_reason = f"Pozisyon açıldı: {cand.symbol}"
                return
            last_reason = f"{cand.symbol}: {reason}"
        self._set_reason(f"İşlem açılmadı - {last_reason}")

    def _open_position(self, cand: ScoreResult, size_mult: float,
                       extra_cost_pct: float = 0.0) -> tuple[bool, str]:
        filters = self.client.get_filters(cand.symbol)
        book = self.client.book_ticker(cand.symbol)
        ask, bid = float(book["askPrice"]), float(book["bidPrice"])
        # İğne/ani hareket önlemleri: emir anında defter ve fiyat tekrar kontrol edilir
        if bid <= 0 or ask <= 0:
            return False, "Emir defteri boş"
        spread_now = (ask - bid) / ((ask + bid) / 2)
        if spread_now > self.s.max_spread_pct:
            return False, f"Anlık spread yüksek ({spread_now * 100:.2f}%)"
        if cand.price > 0 and abs(ask / cand.price - 1) > self.s.max_entry_drift_pct:
            return False, "Fiyat taramadan sonra hızlı hareket etti (iğne/ani hareket)"
        est_entry = ask * (1 + self.s.slippage_pct)
        try:
            est_stop = self._entry_stop(est_entry, cand)
        except ValueError:
            return False, "Geçerli stop hesaplanamadı"
        edge = expected_edge(cand.score, est_entry, est_stop, self.s.reward_risk_ratio,
                             self.s.round_trip_cost_pct + extra_cost_pct,
                             self.s.min_expected_edge_pct, self.s.min_cost_coverage)
        if not edge.ok:
            return False, edge.reason
        snap = self.snapshot or self.portfolio.snapshot(self.prices, self.db.open_trades(self.book))
        size = self.risk.position_size(snap.equity_quote, snap.quote_free, est_entry, est_stop, size_mult)
        if not size.ok:
            return False, size.reason
        pre_free = self._free_balance(filters.base_asset) if self.s.is_live else None
        try:
            fill = self.orders.buy(cand.symbol, size.quantity, filters)
        except OrderError as exc:
            return False, str(exc)

        # Miktar ve maliyetin TEK kaynağı borsanın gerçekleşme raporudur. Bakiye borsada geç
        # yansıyabilir (ve eski toz bakiye içerebilir); bu yüzden bakiyeye göre miktar
        # KÜÇÜLTÜLMEZ. Burada yalnızca yeni bakiyenin yansıması kısa süre beklenir.
        if self.s.is_live:
            expected = (pre_free or 0.0) + fill.quantity * (1 - QTY_TOLERANCE)
            self._free_balance(filters.base_asset, retries=5, minimum=expected)
        stop = self._entry_stop(fill.avg_price, cand)
        trade = Trade(
            symbol=cand.symbol, base_asset=filters.base_asset, mode=self.book, status="OPEN",
            quantity=fill.quantity, entry_price=fill.avg_price, entry_quote=fill.quote_net,
            entry_fee_usdt=fill.fee_usdt, entry_external_fee_usdt=fill.external_fee_usdt,
            entry_time=utcnow(), entry_order_id=fill.order_id, score_at_entry=cand.score,
            atr_at_entry=cand.atr, current_atr=cand.atr, initial_stop=stop, stop_price=stop,
            highest_price=fill.avg_price,
        )
        trade = self.db.add_trade(trade)
        self.db.log_decision("TRADE", (
            f"ALIŞ {cand.symbol} miktar={fill.quantity:.8g} fiyat={fill.avg_price:.8g} "
            f"stop={stop:.8g} puan={cand.score:.1f} beklenen net={edge.expected_net_pct * 100:.2f}% "
            f"risk≈{size.risk_usdt:.2f} {self.quote}"), data={"components": cand.components})

        if self.s.is_live and self.s.place_exchange_stop:
            order_id, stop_px = self._safe_place_stop(trade, filters)
            if order_id is None and self._last_stop_failure == "BALANCE":
                # Bakiye henüz yansımadı: pozisyon kapatılmaz; yazılım stopu aktif, borsa
                # stopu her döngüde yeniden denenir.
                self.db.log_decision("SAFETY", f"{cand.symbol} borsa stopu bakiye yansıyınca konacak; "
                                               f"yazılım stopu aktif", level="WARNING")
                self._update_daily(utcnow(), snap.equity_quote)
                return True, "OK (borsa stopu bekleniyor)"
            if order_id is None and filters.supports("STOP_LOSS_LIMIT"):
                self.db.log_decision("SAFETY", f"{cand.symbol} koruyucu stop yerleştirilemedi; "
                                               f"pozisyon güvenlik için kapatılıyor", level="ERROR")
                try:
                    self.close_trade(trade, "STOP_PLACEMENT_FAILED")
                except (BinanceAPIError, OrderError) as exc:
                    self.db.log_decision("SAFETY", f"{cand.symbol} kapatılamadı: {exc}. Yazılım stopu "
                                                   f"aktif, borsa stopu her döngüde yeniden denenecek.",
                                         level="ERROR")
                    return True, "Pozisyon açık kaldı (stop yeniden denenecek)"
                return False, "Koruyucu stop yerleştirilemedi"
            trade.stop_order_id, trade.stop_order_price = order_id, stop_px
            self.db.save_trade(trade)
        self._update_daily(utcnow(), snap.equity_quote)
        return True, "OK"

    def _free_balance(self, asset: str, retries: int = 0, minimum: float = 0.0) -> float | None:
        """Varlığın serbest bakiyesi (okunamazsa None).

        Borsa bakiyeyi geç yansıtabilir: `minimum` verilirse bakiye en az bu değere ulaşana kadar
        `retries` kez tekrar okunur; ulaşmazsa son okunan değer döner (asla uydurulmaz).
        """
        free = None
        for attempt in range(retries + 1):
            try:
                free = self.client.balances().get(asset, {}).get("free", 0.0)
            except BinanceAPIError as exc:
                logger.warning("%s bakiyesi okunamadı: %s", asset, exc)
                free = None
            if free and free >= minimum:
                return free
            if attempt < retries:
                time.sleep(1)
        return free

    def _entry_stop(self, entry: float, cand: ScoreResult) -> float:
        recent_low = cand.indicators.recent_low if cand.indicators else 0.0
        return wick_aware_stop(entry, cand.atr, self.s.stop_atr_multiplier, recent_low,
                               self.s.stop_swing_buffer_atr, self.s.max_stop_atr)

    def _safe_place_stop(self, trade: Trade, filters) -> tuple[str | None, float | None]:
        """Borsaya koruyucu stop koyar. Pozisyonun tamamını kapsamayan stop KONMAZ.

        - Serbest bakiye kayıtlı miktarın %2'sinden fazla eksikse (bakiye gecikmesi, eski toz,
          harici satış) stop konmaz, miktar/maliyet değiştirilmez; sonraki döngüde tekrar denenir.
        - Fark %2 içindeyse (baz varlıktan kesilen komisyon/yuvarlama) stop serbest bakiye kadar
          konur ve kayıtlı miktar buna eşitlenir (maliyet aynı kalır; komisyon maliyete dahildir).
        """
        self._last_stop_failure = None
        stop_qty = trade.quantity
        if self.s.is_live and self.s.place_exchange_stop:
            need = float(filters.round_qty(trade.quantity))
            free = self._free_balance(trade.base_asset, retries=3, minimum=need)
            if free is None:
                self._last_stop_failure = "BALANCE"
                return None, None
            if free < need:
                if free >= trade.quantity * (1 - QTY_TOLERANCE):
                    stop_qty = free
                    trade.quantity = float(filters.round_qty(free))
                else:
                    self._last_stop_failure = "BALANCE"
                    self._note_stop_unverified(trade, free)
                    return None, None
        last_error = None
        for attempt in range(2):
            try:
                result = self.orders.place_stop(trade.symbol, stop_qty, trade.stop_price, filters)
                self._stop_errors.pop(trade.symbol, None)
                self._unverified_cycles.pop(trade.id, None)
                if result[0] is None and self.s.is_live and self.s.place_exchange_stop:
                    self._last_stop_failure = "ORDER" if filters.supports("STOP_LOSS_LIMIT") else None
                    reason = self.orders.last_stop_error or "bilinmeyen sebep"
                    if self._stop_errors.get(trade.symbol) != reason:
                        self._stop_errors[trade.symbol] = reason
                        self.db.log_decision("STOP", f"{trade.symbol} borsa stop emri konamadı: {reason}",
                                             level="ERROR")
                return result
            except BinanceAPIError as exc:
                last_error = str(exc)
                logger.warning("%s stop yerleştirme hatası (deneme %d): %s", trade.symbol, attempt + 1, exc)
        insufficient = last_error and ("insufficient" in last_error.lower() or "2202" in last_error
                                       or "-2010" in last_error)
        self._last_stop_failure = "BALANCE" if insufficient else "ORDER"
        if insufficient:
            self._note_stop_unverified(trade, None)
        if last_error and self._stop_errors.get(trade.symbol) != last_error:
            self._stop_errors[trade.symbol] = last_error
            self.db.log_decision("STOP", f"{trade.symbol} borsa stop emri yerleştirilemedi: {last_error}",
                                 level="ERROR")
        return None, None

    def _note_stop_unverified(self, trade: Trade, free: float | None) -> None:
        count = self._unverified_cycles.get(trade.id, 0) + 1
        self._unverified_cycles[trade.id] = count
        if count == 1:
            self.db.log_decision("STOP", f"{trade.symbol} bakiye pozisyonu doğrulamıyor (serbest={free}, "
                                         f"kayıt={trade.quantity:.8g}); stop konmadı, tekrar denenecek. "
                                         f"Miktar ve maliyet korunuyor.", level="WARNING")
        if count == MAX_UNVERIFIED_STOP_CYCLES:
            self.flag_review(f"{trade.symbol} (#{trade.id}): borsa bakiyesi kayıtlı miktarı "
                             f"{count} döngüdür doğrulamıyor; borsa stopu konamadı")

    # ------------------------------------------------------ inceleme bayrağı
    def review_flags(self) -> list[str]:
        return list(self.db.get_state(REVIEW_KEY, []) or [])

    def flag_review(self, message: str) -> None:
        flags = self.review_flags()
        if message not in flags:
            flags.append(message)
            self.db.set_state(REVIEW_KEY, flags)
            self.db.log_decision("SAFETY", f"İNCELEME GEREKİYOR: {message}. Yeni işlem açılmayacak.",
                                 level="ERROR")

    def clear_review(self) -> None:
        self.db.set_state(REVIEW_KEY, [])
        self.db.log_decision("SAFETY", "İnceleme bayrakları temizlendi", level="WARNING")

    # ------------------------------------------------------ pozisyon yönetimi
    def _manage_position(self, t: Trade, filters, now: datetime) -> None:
        if self.s.is_live and t.stop_order_id:
            status, fill = self.orders.check_stop_filled(t.symbol, t.stop_order_id)
            if status == "UNKNOWN":
                count = self._unverified_cycles.get(-t.id, 0) + 1
                self._unverified_cycles[-t.id] = count
                if count == MAX_UNVERIFIED_STOP_CYCLES:
                    self.flag_review(f"{t.symbol} (#{t.id}): borsa stop emrinin durumu {count} döngüdür "
                                     f"okunamıyor")
            else:
                self._unverified_cycles.pop(-t.id, None)
            if status == "FILLED" and fill is not None:
                t.stop_order_id = None
                if self._apply_fill(t, fill, "STOP_LOSS_EXCHANGE", filters):
                    return
            if status == "GONE" or (status == "FILLED" and t.status == "OPEN"):
                if status == "GONE" and fill is not None:
                    self._apply_fill(t, fill, "STOP_LOSS_EXCHANGE", filters)
                    if t.status != "OPEN":
                        return
                t.stop_order_id, t.stop_order_price = self._safe_place_stop(t, filters)
                self.db.save_trade(t)
        elif self.s.is_live and self.s.place_exchange_stop and filters.supports("STOP_LOSS_LIMIT"):
            # Borsa stopu yoksa (önceki yerleştirme başarısız) her döngüde yeniden dene
            t.stop_order_id, t.stop_order_price = self._safe_place_stop(t, filters)
            if t.stop_order_id:
                self.db.log_decision("STOP", f"{t.symbol} koruyucu stop emri yerleştirildi")
            self.db.save_trade(t)

        bid = float(self.client.book_ticker(t.symbol)["bidPrice"])
        candles = Candles.from_klines(self.client.klines(t.symbol, self.s.kline_interval,
                                                         limit=self.s.kline_limit))
        ind = compute_indicators(candles, self.s.atr_period)
        t.current_atr = ind.atr

        upd = compute_stop_update(
            entry=t.entry_price, current_stop=t.stop_price, highest=t.highest_price, price=bid,
            atr_value=ind.atr, breakeven_active=t.breakeven_active, trailing_active=t.trailing_active,
            breakeven_trigger_atr=self.s.breakeven_trigger_atr,
            trailing_trigger_atr=self.s.trailing_trigger_atr,
            trailing_atr_multiplier=self._trailing_multiplier(t, bid),
            round_trip_cost_pct=self.s.round_trip_cost_pct,
        )
        t.highest_price, t.breakeven_active, t.trailing_active = upd.highest, upd.breakeven_active, upd.trailing_active
        if upd.changed:
            self.db.log_decision("STOP", f"{t.symbol} stop yükseltildi {t.stop_price:.8g} -> {upd.stop:.8g}"
                                         f"{' (trailing)' if upd.trailing_active else ' (başa baş)'}")
            t.stop_price = upd.stop

        if stop_hit(bid, t.stop_price):
            reason = "TRAILING_STOP" if t.trailing_active else ("BREAKEVEN_STOP" if t.breakeven_active else "STOP_LOSS")
            self.close_trade(t, reason)
            return
        if trend_broken(ind):
            self.close_trade(t, "TREND_BROKEN")
            return
        if momentum_negative(ind):
            self.close_trade(t, "MOMENTUM_NEGATIVE")
            return

        if self._partial_tp_due(t, bid):
            self._take_partial_profit(t, filters, bid)
            self.db.save_trade(t)
            return

        if upd.changed and self.s.is_live and t.stop_order_id and self._stop_move_worthwhile(t, filters):
            filled = self.orders.cancel_stop(t.symbol, t.stop_order_id, t.base_asset)
            t.stop_order_id, t.stop_order_price = None, None
            if filled is not None and self._apply_fill(t, filled, "STOP_LOSS_EXCHANGE", filters):
                return
            t.stop_order_id, t.stop_order_price = self._safe_place_stop(t, filters)
        self.db.save_trade(t)

    # ------------------------------------------------------ kâr yönetimi
    @staticmethod
    def _initial_risk(t: Trade) -> float:
        """Birim başına başlangıç riski (R) = giriş - ilk stop."""
        return max(t.entry_price - t.initial_stop, 0.0)

    def _trailing_multiplier(self, t: Trade, price: float) -> float:
        r = self._initial_risk(t)
        peak = max(t.highest_price, price)
        if r > 0 and peak - t.entry_price >= self.s.trail_tighten_after_r * r:
            return self.s.trailing_atr_multiplier_tight
        return self.s.trailing_atr_multiplier

    def _partial_key(self, t: Trade) -> str:
        return f"partial_tp_done:{t.id}"

    def partial_taken(self, t: Trade) -> bool:
        return bool(self.db.get_state(self._partial_key(t), False))

    def _partial_tp_due(self, t: Trade, bid: float) -> bool:
        if not self.s.partial_take_profit_enabled or self.partial_taken(t):
            return False
        r = self._initial_risk(t)
        return r > 0 and bid >= t.entry_price + self.s.partial_tp_r_multiple * r

    def _take_partial_profit(self, t: Trade, filters, bid: float) -> bool:
        """Pozisyonun bir kısmını satar, kalanı için borsa stopunu yeni miktarla yeniden koyar."""
        qty_part = float(filters.round_qty(t.quantity * self.s.partial_tp_fraction, market=True))
        min_notional = float(filters.min_notional or 0)
        if qty_part <= 0 or qty_part * bid < min_notional or (t.quantity - qty_part) * bid < min_notional:
            self.db.set_state(self._partial_key(t), True)
            self.db.log_decision("TAKE_PROFIT", f"{t.symbol} kısmi kâr atlandı: parçalar minimum emir "
                                                f"tutarının altında kalıyor")
            return False

        available = None
        if self.s.is_live:
            filled = self.orders.cancel_stop(t.symbol, t.stop_order_id, t.base_asset)
            t.stop_order_id, t.stop_order_price = None, None
            if filled is not None and self._apply_fill(t, filled, "STOP_LOSS_EXCHANGE", filters):
                return True
            available = self._free_balance(t.base_asset, retries=2,
                                           minimum=float(filters.round_qty(qty_part)))
        try:
            sold = self.orders.sell(t.symbol, qty_part, filters, available=available)
        except (BinanceAPIError, OrderError) as exc:
            sold = None
            self.db.log_decision("TAKE_PROFIT", f"{t.symbol} kısmi satış başarısız: {exc}", level="ERROR")
        if sold is not None and sold.quantity > 0:
            self._record_partial(t, sold, PARTIAL_TP_REASON)
            self.db.set_state(self._partial_key(t), True)
        if self.s.is_live and self.s.place_exchange_stop and t.status == "OPEN":
            t.stop_order_id, t.stop_order_price = self._safe_place_stop(t, filters)
        return sold is not None

    def _record_partial(self, t: Trade, sold: FillResult, reason: str = PARTIAL_TP_REASON) -> None:
        """Satılan kısmı ayrı kapalı kayıt yapar; kalan miktar ve ORANSAL maliyet açık kalır."""
        if not reason.startswith(PARTIAL_PREFIX):
            reason = f"{PARTIAL_PREFIX}_{reason}"
        frac = min(sold.quantity / t.quantity, 1.0) if t.quantity else 1.0
        entry_quote = t.entry_quote * frac
        entry_ext = (t.entry_external_fee_usdt or 0.0) * frac
        pnl = sold.quote_net - entry_quote - entry_ext - sold.external_fee_usdt
        part = Trade(
            symbol=t.symbol, base_asset=t.base_asset, mode=t.mode, status="CLOSED",
            quantity=sold.quantity, entry_price=t.entry_price, entry_quote=entry_quote,
            entry_fee_usdt=(t.entry_fee_usdt or 0.0) * frac, entry_external_fee_usdt=entry_ext,
            entry_time=t.entry_time, entry_order_id=t.entry_order_id, score_at_entry=t.score_at_entry,
            atr_at_entry=t.atr_at_entry, current_atr=t.current_atr, initial_stop=t.initial_stop,
            stop_price=t.stop_price, highest_price=t.highest_price, exit_price=sold.avg_price,
            exit_quote=sold.quote_net, exit_fee_usdt=sold.fee_usdt,
            exit_external_fee_usdt=sold.external_fee_usdt, exit_time=utcnow(),
            exit_order_id=sold.order_id, exit_reason=reason, pnl_usdt=pnl,
            pnl_pct=pnl / entry_quote * 100 if entry_quote else 0.0,
            notes=f"İşlem #{t.id} için kısmi kapanış ({reason})",
        )
        self.db.add_trade(part)
        t.quantity = max(0.0, t.quantity - sold.quantity)
        t.entry_quote -= entry_quote
        t.entry_fee_usdt = (t.entry_fee_usdt or 0.0) * (1 - frac)
        t.entry_external_fee_usdt = (t.entry_external_fee_usdt or 0.0) * (1 - frac)
        category = "TAKE_PROFIT" if reason == PARTIAL_TP_REASON else "TRADE"
        label = "KISMİ KÂR" if reason == PARTIAL_TP_REASON else f"KISMİ KAPANIŞ ({reason})"
        self.db.log_decision(category, f"{label} {t.symbol} miktar={sold.quantity:.8g} "
                                       f"fiyat={sold.avg_price:.8g} PNL={pnl:.4f} {self.quote}; "
                                       f"kalan={t.quantity:.8g}")
        if self.snapshot is not None:
            self._update_daily(utcnow(), self.snapshot.equity_quote)

    def _stop_move_worthwhile(self, t: Trade, filters) -> bool:
        """Borsa stopunu yalnızca yuvarlanmış fiyat anlamlı ölçüde yükseliyorsa yenile.

        Her küçük yükselişte iptal/yeniden koyma gereksiz emir trafiği ve kısa korumasız anlar
        yaratır. Yazılım stopu her zaman kesin seviyeyi (t.stop_price) kullanır.
        """
        new_px = float(filters.round_price(t.stop_price))
        old_px = t.stop_order_price or 0.0
        if old_px <= 0:
            return True
        if new_px <= old_px:
            return False
        moved_to_breakeven = old_px < t.entry_price <= new_px
        return moved_to_breakeven or (new_px - old_px) / old_px >= self.s.min_stop_update_pct

    def unrealized_pnl(self, t: Trade) -> float:
        price = self.prices.get(t.symbol, t.entry_price)
        exit_value = t.quantity * price * (1 - self.orders.fee_rate - self.s.slippage_pct)
        return exit_value - t.entry_quote - (t.entry_external_fee_usdt or 0.0)

    # ---------------------------------------------------------------- kapanış
    def close_trade(self, t: Trade, reason: str) -> bool:
        with self._lock:
            filters = self.client.get_filters(t.symbol)
            self._check_consistency(t)
            filled = self.orders.cancel_stop(t.symbol, t.stop_order_id, t.base_asset)
            t.stop_order_id, t.stop_order_price = None, None
            if filled is not None and self._apply_fill(t, filled, reason, filters):
                return True
            remaining = t.quantity
            available = None
            if self.s.is_live:
                available = self._free_balance(t.base_asset, retries=2,
                                               minimum=float(filters.round_qty(remaining)))
            sold = self.orders.sell(t.symbol, remaining, filters, available=available) if remaining > 0 else None
            if sold is not None and sold.quantity > 0:
                if self._apply_fill(t, sold, reason, filters):
                    return True
                # Eksik satış: satılan kısım kaydedildi, kalan miktar/maliyet AÇIK; stop yeniden konur
                t.stop_order_id, t.stop_order_price = self._safe_place_stop(t, filters)
                self.db.save_trade(t)
                self.db.log_decision("SAFETY", f"{t.symbol} kısmen satıldı; kalan {t.quantity:.8g} açık "
                                               f"tutuluyor", level="ERROR")
                return False
            bid = float(self.client.book_ticker(t.symbol)["bidPrice"])
            if self._is_dust(t.quantity, bid, filters):
                # Gerçek toz: satılamaz; değeri piyasa fiyatıyla tahmin edilerek kapatılır
                est = FillResult(bid, t.quantity, t.quantity * bid * (1 - self.orders.fee_rate), 0.0)
                self._finalize_close(t, est, reason, note="Satılamayan toz bakiye; değer tahmini")
                return True
            # Gerçek bir pozisyon satılamadı: kayıt AÇIK kalır, sonraki döngüde tekrar denenir
            self.db.save_trade(t)
            raise OrderError(f"{t.symbol} satılamadı (serbest bakiye={available}); pozisyon açık "
                             f"bırakıldı, tekrar denenecek")

    @staticmethod
    def _is_dust(qty: float, price: float, filters) -> bool:
        return qty <= 0 or qty * price < float(filters.min_notional or 0) \
            or float(filters.round_qty(qty, market=True)) <= 0

    def _check_consistency(self, t: Trade) -> None:
        """Kayıtlı miktar ile maliyet birbirini tutmuyorsa otomatik kapanışı engeller."""
        if t.entry_quote > 0 and t.entry_price > 0 and t.quantity * t.entry_price < 0.5 * t.entry_quote:
            msg = (f"{t.symbol} (#{t.id}): kayıtlı miktar ({t.quantity:.8g}) maliyetle "
                   f"({t.entry_quote:.4f}) tutarsız")
            self.flag_review(msg)
            raise ReviewRequired(msg + "; otomatik kapanış yapılmadı")

    def _apply_fill(self, t: Trade, fill: FillResult, reason: str, filters) -> bool:
        """Bir satış dolumunu uygular. Pozisyon tamamen kapandıysa True.

        Dolum kayıtlı miktarın tamamını (komisyon/yuvarlama payı ve satılamaz toz hariç)
        kapsıyorsa işlem kapatılır; kapsamıyorsa satılan kısım oransal maliyetle ayrı kayıt
        olur ve kalan pozisyon açık kalır.
        """
        self._check_consistency(t)
        leftover = t.quantity - fill.quantity
        price = fill.avg_price or t.entry_price
        if leftover <= t.quantity * QTY_TOLERANCE or self._is_dust(leftover, price, filters):
            self._finalize_close(t, fill, reason)
            return True
        self._record_partial(t, fill, reason)
        self.db.save_trade(t)
        return False

    def _finalize_close(self, t: Trade, fill: FillResult, reason: str, note: str | None = None) -> None:
        t.status = "CLOSED"
        t.exit_price = fill.avg_price
        t.exit_quote = fill.quote_net
        t.exit_fee_usdt = fill.fee_usdt
        t.exit_external_fee_usdt = fill.external_fee_usdt
        t.exit_time = utcnow()
        t.exit_order_id = fill.order_id
        t.exit_reason = reason
        t.stop_order_id = None
        pnl = fill.quote_net - t.entry_quote - (t.entry_external_fee_usdt or 0.0) - fill.external_fee_usdt
        t.pnl_usdt = pnl
        t.pnl_pct = pnl / t.entry_quote * 100 if t.entry_quote else 0.0
        if note:
            t.notes = (t.notes or "") + f" | {note}"
        self.db.save_trade(t)
        self.db.log_decision("TRADE", f"SATIŞ {t.symbol} sebep={reason} fiyat={fill.avg_price:.8g} "
                                      f"PNL={pnl:.4f} {self.quote} ({t.pnl_pct:.2f}%)")
        if self.snapshot is not None:
            self._update_daily(utcnow(), self.snapshot.equity_quote)

    def close_all_positions(self, reason: str = "MANUAL_CLOSE_ALL") -> list[str]:
        results: list[str] = []
        with self._lock:
            self.client.load_exchange_info()
            for t in self.db.open_trades(self.book):
                try:
                    self.close_trade(t, reason)
                    results.append(f"{t.symbol} kapatıldı")
                except (BinanceAPIError, OrderError) as exc:
                    results.append(f"{t.symbol} kapatılamadı: {exc}")
                    self.db.log_decision("TRADE", f"{t.symbol} kapatılamadı: {exc}", level="ERROR")
        return results

    # ------------------------------------------------------------- rotasyon
    def _try_rotation(self, scan: ScanResult, now: datetime, day_start_equity: float,
                      unrealized: float) -> None:
        if not self.s.rotation_enabled or self.emergency:
            return
        best = scan.best
        if best is None:
            return
        min_score, _ = self._min_score()
        if best.score < min_score or (self.regime and not self.regime.allows_new_trades):
            return
        if self.db.closed_since_by_reason(utc_day_start(now), "ROTATION", self.book) >= \
                self.s.rotation_max_per_day:
            return
        for t in self.db.open_trades(self.book):
            if t.symbol == best.symbol:
                continue
            if now - t.entry_time < timedelta(minutes=self.s.rotation_min_hold_minutes):
                continue
            if self.s.rotation_require_profit and not (t.breakeven_active and self.unrealized_pnl(t) > 0):
                # Zarardaki/başa baş korumasız pozisyon rotasyonla satılmaz; stop kuralları yönetir
                continue
            try:
                current = self._score_symbol(t.symbol, scan)
            except BinanceAPIError:
                continue
            diff = best.score - current.score
            if diff < self.s.rotation_min_score_diff:
                continue
            open_after = len(self.db.open_trades(self.book)) - 1
            check = self.risk.can_open_trade(now, open_after, day_start_equity, unrealized, self.emergency)
            if not check.allowed:
                return
            est_entry = best.price
            try:
                est_stop = initial_stop_price(est_entry, best.atr, self.s.stop_atr_multiplier)
            except ValueError:
                return
            edge = expected_edge(best.score, est_entry, est_stop, self.s.reward_risk_ratio,
                                 self.s.round_trip_cost_pct * 1.5, self.s.min_expected_edge_pct,
                                 self.s.min_cost_coverage)
            if not edge.ok:
                return
            self.db.log_decision("ROTATION", f"{t.symbol} ({current.score:.1f}) -> {best.symbol} "
                                             f"({best.score:.1f}), fark {diff:.1f}")
            self.close_trade(t, "ROTATION")

    def _execute_holding_decisions(self, decisions: list[HoldingDecision]) -> None:
        for d in decisions:
            if not d.executable or d.symbol is None:
                continue
            try:
                filters = self.client.get_filters(d.symbol)
                bal = self.client.balances().get(d.asset, {"free": 0.0})
                qty = bal["free"] * (self.s.partial_sell_fraction if d.decision == "PARTIAL_SELL" else 1.0)
                fill = self.orders.sell(d.symbol, qty, filters, available=bal["free"])
                if fill is not None:
                    self.db.log_decision("HOLDING", f"{d.decision} {d.symbol} miktar={fill.quantity:.8g} "
                                                    f"tutar={fill.quote_net:.2f} {self.quote}: {d.reason}")
                    self.portfolio._confirm.pop(d.asset, None)
            except (BinanceAPIError, OrderError) as exc:
                self.db.log_decision("HOLDING", f"{d.symbol} satılamadı: {exc}", level="ERROR")

    # -------------------------------------------------------- günlük istatistik
    def _day_key(self, now: datetime) -> str:
        day = now.strftime("%Y-%m-%d")
        return day if self.book in ("DRY_RUN", "LIVE") else f"{day}@{self.book}"

    def _update_daily(self, now: datetime, equity: float):
        day = self._day_key(now)
        since = utc_day_start(now)
        stat = self.db.get_daily_stat(day)
        closed_today = [t for t in self.db.closed_trades_desc(limit=500, mode=self.book)
                        if t.exit_time and t.exit_time >= since]
        fields = dict(
            end_equity=equity,
            realized_pnl=self.db.realized_pnl_since(since, self.book),
            trades_opened=self.db.trades_opened_since(since, self.book),
            trades_closed=len(closed_today),
            wins=sum(1 for t in closed_today if (t.pnl_usdt or 0) > 0),
            losses=sum(1 for t in closed_today if (t.pnl_usdt or 0) < 0),
            fees_usdt=sum(t.total_fee_usdt for t in closed_today),
        )
        if stat is None:
            fields["start_equity"] = equity
        return self.db.upsert_daily_stat(day, **fields)

    # ----------------------------------------------------------------- panel
    def status(self) -> dict:
        now = utcnow()
        open_trades = self.db.open_trades(self.book)
        positions = []
        unrealized_total = 0.0
        for t in open_trades:
            price = self.prices.get(t.symbol, t.entry_price)
            upnl = self.unrealized_pnl(t)
            unrealized_total += upnl
            positions.append({
                "id": t.id, "symbol": t.symbol, "quantity": t.quantity,
                "entry_price": t.entry_price, "current_price": price,
                "initial_stop": t.initial_stop, "stop_price": t.stop_price,
                "trailing_active": t.trailing_active, "breakeven_active": t.breakeven_active,
                "trailing_stop": t.stop_price if t.trailing_active else None,
                "highest_price": t.highest_price, "pnl_usdt": round(upnl, 4),
                "pnl_pct": round(upnl / t.entry_quote * 100, 3) if t.entry_quote else 0.0,
                "score_at_entry": t.score_at_entry, "entry_time": t.entry_time.isoformat(),
                "exchange_stop_order": t.stop_order_id,
                "partial_taken": self.partial_taken(t),
            })
        stat = self.db.get_daily_stat(self._day_key(now))
        streak, _ = self.risk.consecutive_losses()
        cooldown = self.risk.cooldown_until()
        snap = self.snapshot
        return {
            "running": self.running,
            "mode": self.mode,
            "exchange": self.exchange,
            "accounts": list(self.account_views.values()),
            "emergency_stop": self.emergency,
            "regime": self.regime.regime.value if self.regime else None,
            "regime_reason": self.regime.reason if self.regime else None,
            "equity_usdt": round(snap.equity_usdt, 4) if snap else None,
            "usdt_free": round(snap.quote_free, 4) if snap else None,
            "quote_free": round(snap.quote_free, 4) if snap else None,
            "quote_asset": self.quote,
            "fx": self.fx,
            "portfolio": snap.to_dict() if snap else None,
            "real_portfolio": self.real_snapshot.to_dict() if self.real_snapshot else None,
            "account_status": {
                "has_api_keys": self.client.has_keys,
                "error": self.portfolio.last_account_error,
            },
            "daily_pnl": round(self.risk.daily_pnl(now, unrealized_total), 4),
            "total_pnl": round(self.db.total_realized_pnl(self.book) + unrealized_total, 4),
            "realized_pnl": round(self.db.total_realized_pnl(self.book), 4),
            "total_fees": round(self.db.total_fees(self.book), 4),
            "day_start_equity": stat.start_equity if stat else None,
            "positions": positions,
            "no_trade_reason": self.no_trade_reason,
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "last_error": self.last_error,
            "trades_today": self.risk.trades_today(now),
            "consecutive_losses": streak,
            "cooldown_until": cooldown.isoformat() if cooldown and cooldown > now else None,
            "holding_decisions": [d.to_dict() for d in self.holding_decisions],
            "review_required": self.review_flags(),
            "settings": self.s.public_summary(),
        }
