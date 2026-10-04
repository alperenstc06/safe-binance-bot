"""Emir yönetimi.

DRY_RUN: Emirler gerçek defter fiyatı (ask/bid) + slippage + komisyon ile simüle edilir,
         kağıt (paper) USDT bakiyesi veritabanında tutulur. Binance'e EMİR GÖNDERİLMEZ.
LIVE:    Binance Spot MARKET emirleri ve borsa tarafında koruyucu STOP_LOSS_LIMIT emri.

Tüm miktar ve fiyatlar exchangeInfo filtrelerine göre yuvarlanır ve doğrulanır.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from binance_client.client import BinanceAPIError
from binance_client.filters import FilterError, SymbolFilters

logger = logging.getLogger(__name__)

PAPER_USDT_KEY = "paper_usdt"


class OrderError(RuntimeError):
    pass


@dataclass
class FillResult:
    """Bir alış veya satışın sonucu.

    quantity: hesaba giren (alış) / hesaptan çıkan (satış) net baz varlık miktarı
    quote_net: alışta harcanan, satışta alınan net USDT (USDT komisyonu dahil edilmiş)
    fee_usdt: toplam komisyonun USDT karşılığı (raporlama)
    external_fee_usdt: USDT ve baz varlık dışında (ör. BNB) ödenen komisyonun USDT karşılığı
    """

    avg_price: float
    quantity: float
    quote_net: float
    fee_usdt: float
    external_fee_usdt: float = 0.0
    order_id: str | None = None

    @staticmethod
    def combine(a: "FillResult | None", b: "FillResult | None") -> "FillResult | None":
        if a is None:
            return b
        if b is None:
            return a
        qty = a.quantity + b.quantity
        avg = (a.avg_price * a.quantity + b.avg_price * b.quantity) / qty if qty else 0.0
        return FillResult(avg, qty, a.quote_net + b.quote_net, a.fee_usdt + b.fee_usdt,
                          a.external_fee_usdt + b.external_fee_usdt, b.order_id or a.order_id)


def commission_usdt(fee: float, asset: str, base_asset: str, fill_price: float,
                    price_lookup, quote: str = "USDT") -> float:
    """Komisyonun işlem para birimi (quote) karşılığı."""
    if fee <= 0:
        return 0.0
    if asset == quote:
        return fee
    if asset == base_asset:
        return fee * fill_price
    try:
        return fee * price_lookup(f"{asset}{quote}")
    except Exception:  # fiyat bulunamazsa raporlamada 0 kabul edilir
        logger.warning("Komisyon varlığı %s için %s fiyatı alınamadı", asset, quote)
        return 0.0


def parse_market_fill(resp: dict, side: str, base_asset: str, price_lookup,
                      quote: str = "USDT") -> FillResult:
    executed = float(resp.get("executedQty", 0) or 0)
    quote_amt = float(resp.get("cummulativeQuoteQty", 0) or 0)
    base_fee = usdt_fee = external = total = 0.0
    for f in resp.get("fills", []) or []:
        fee, asset, price = float(f.get("commission", 0)), f.get("commissionAsset", ""), float(f["price"])
        value = commission_usdt(fee, asset, base_asset, price, price_lookup, quote)
        total += value
        if asset == base_asset:
            base_fee += fee
        elif asset == quote:
            usdt_fee += fee
        else:
            external += value
    avg = quote_amt / executed if executed else 0.0
    if side == "BUY":
        return FillResult(avg, executed - base_fee, quote_amt + usdt_fee, total, external,
                          str(resp.get("orderId")))
    return FillResult(avg, executed + 0.0, quote_amt - usdt_fee - base_fee * avg, total, external,
                      str(resp.get("orderId")))


class OrderManager:
    def __init__(self, client, db, settings, book: str = "DRY_RUN"):
        self.client = client
        self.db = db
        self.s = settings
        self.live = settings.is_live
        self.fee_rate = settings.fee_rate
        # Her borsanın kağıt bakiyesi ayrı ve kendi işlem para biriminde tutulur
        self.quote = settings.quote_asset
        exchange = book.split("@", 1)[1] if "@" in book else None
        if self.quote == "USDT":
            self.paper_key = PAPER_USDT_KEY if exchange is None else f"{PAPER_USDT_KEY}@{exchange}"
            if not self.live and self.db.get_state(self.paper_key) is None:
                self.db.set_state(self.paper_key, float(settings.dry_run_start_balance))
        else:
            self.paper_key = f"paper_{self.quote.lower()}@{exchange or 'BINANCE_GLOBAL'}"

    def ensure_paper_balance(self, fx: float) -> None:
        """DRY_RUN başlangıç bakiyesini (USDT) işlem para birimine çevirerek bir kez yazar."""
        if not self.live and self.db.get_state(self.paper_key) is None:
            self.db.set_state(self.paper_key, round(float(self.s.dry_run_start_balance) * fx, 8))

    def refresh_fee_rate(self) -> None:
        if self.live and self.client.has_keys:
            self.fee_rate = self.client.taker_fee_rate(self.s.fee_rate)

    # --- Kağıt bakiye ---
    @property
    def paper_balance(self) -> float:
        """Kağıt hesaptaki nakit (işlem para birimi cinsinden)."""
        return float(self.db.get_state(self.paper_key, self.s.dry_run_start_balance))

    @property
    def paper_usdt(self) -> float:
        return self.paper_balance

    def _set_paper_usdt(self, value: float) -> None:
        self.db.set_state(self.paper_key, round(value, 8))

    # --- Alış ---
    def buy(self, symbol: str, quantity: float, filters: SymbolFilters) -> FillResult:
        book = self.client.book_ticker(symbol)
        ask = float(book["askPrice"])
        if ask <= 0:
            raise OrderError(f"{symbol}: geçersiz ask fiyatı")
        try:
            qty = filters.round_qty(quantity, market=True)
            filters.validate_order(qty, Decimal(str(ask)), market=True)
        except FilterError as exc:
            raise OrderError(str(exc)) from exc

        if not self.live:
            price = ask * (1 + self.s.slippage_pct)
            notional = float(qty) * price
            fee = notional * self.fee_rate
            cost = notional + fee
            if cost > self.paper_usdt + 1e-9:
                raise OrderError(f"Kağıt bakiyede yeterli {self.quote} yok")
            self._set_paper_usdt(self.paper_usdt - cost)
            return FillResult(price, float(qty), cost, fee, 0.0, "DRY-BUY")

        try:
            resp = self.client.market_buy(symbol, qty)
        except BinanceAPIError as exc:
            raise OrderError(f"Alış emri başarısız: {exc}") from exc
        fill = parse_market_fill(resp, "BUY", filters.base_asset, self.client.price, filters.quote_asset)
        if fill.quantity <= 0:
            raise OrderError(f"{symbol}: alış emri gerçekleşmedi ({resp.get('status')})")
        return fill

    # --- Satış ---
    def sell(self, symbol: str, quantity: float, filters: SymbolFilters,
             available: float | None = None) -> FillResult | None:
        """Piyasa satışı. Miktar filtrelere uymuyorsa (toz) None döner."""
        book = self.client.book_ticker(symbol)
        bid = float(book["bidPrice"])
        if bid <= 0:
            raise OrderError(f"{symbol}: geçersiz bid fiyatı")
        qty_raw = min(quantity, available) if available is not None else quantity
        qty = filters.round_qty(qty_raw, market=True)
        try:
            filters.validate_order(qty, Decimal(str(bid)), market=True)
        except FilterError as exc:
            logger.warning("%s satış atlandı: %s", symbol, exc)
            return None

        if not self.live:
            price = bid * (1 - self.s.slippage_pct)
            gross = float(qty) * price
            fee = gross * self.fee_rate
            self._set_paper_usdt(self.paper_usdt + gross - fee)
            return FillResult(price, float(qty), gross - fee, fee, 0.0, "DRY-SELL")

        try:
            resp = self.client.market_sell(symbol, qty)
        except BinanceAPIError as exc:
            raise OrderError(f"Satış emri başarısız: {exc}") from exc
        return parse_market_fill(resp, "SELL", filters.base_asset, self.client.price, filters.quote_asset)

    # --- Borsa tarafı koruyucu stop (yalnızca LIVE) ---
    def place_stop(self, symbol: str, quantity: float, stop: float,
                   filters: SymbolFilters) -> tuple[str | None, float | None]:
        if not self.live or not self.s.place_exchange_stop:
            return None, None
        if not filters.supports("STOP_LOSS_LIMIT"):
            logger.warning("%s STOP_LOSS_LIMIT desteklemiyor; yazılım stopu kullanılacak", symbol)
            return None, None
        qty = filters.round_qty(quantity)
        stop_p = filters.round_price(stop)
        limit_p = filters.round_price(stop * (1 - self.s.stop_limit_offset_pct))
        try:
            filters.validate_price(stop_p)
            filters.validate_price(limit_p)
            filters.validate_order(qty, limit_p)
        except FilterError as exc:
            logger.warning("%s borsa stopu yerleştirilemedi: %s", symbol, exc)
            return None, None
        resp = self.client.stop_loss_limit_sell(symbol, qty, stop_p, limit_p)
        return str(resp.get("orderId")), float(stop_p)

    def cancel_stop(self, symbol: str, order_id: str | None,
                    base_asset: str) -> FillResult | None:
        """Stop emrini iptal eder. Kısmen/tamamen dolmuşsa dolan kısmı FillResult olarak döner."""
        if not self.live or not order_id:
            return None
        try:
            resp = self.client.cancel_order(symbol, order_id)
        except BinanceAPIError:
            try:
                resp = self.client.get_order(symbol, order_id)
            except BinanceAPIError as exc:
                logger.error("%s stop emri durumu okunamadı: %s", symbol, exc)
                return None
        return self._fill_from_order(resp)

    def check_stop_filled(self, symbol: str, order_id: str | None) -> tuple[str, FillResult | None]:
        """(durum, dolum) döner. Durum: NONE / OPEN / FILLED / GONE / UNKNOWN.

        UNKNOWN: emir durumu okunamadı (ağ/API hatası). Bu durumda emir VAR kabul edilir;
        ikinci bir stop konmaz, sonraki döngüde tekrar sorgulanır.
        """
        if not self.live or not order_id:
            return "NONE", None
        try:
            order = self.client.get_order(symbol, order_id)
        except BinanceAPIError as exc:
            if exc.code in (-2013, -2011):  # emir borsada yok
                return "GONE", None
            logger.warning("%s stop emri durumu okunamadı: %s", symbol, exc)
            return "UNKNOWN", None
        status = order.get("status")
        if status == "FILLED":
            return "FILLED", self._fill_from_order(order)
        if status in ("NEW", "PARTIALLY_FILLED"):
            return "OPEN", None
        return "GONE", self._fill_from_order(order)

    def _fill_from_order(self, order: dict) -> FillResult | None:
        executed = float(order.get("executedQty", 0) or 0)
        if executed <= 0:
            return None
        quote = float(order.get("cummulativeQuoteQty", 0) or 0)
        # Sorgu yanıtında komisyon detayı yok: taker oranıyla tahmin edilir.
        fee = quote * self.fee_rate
        return FillResult(quote / executed, executed, quote - fee, fee, 0.0, str(order.get("orderId")))
