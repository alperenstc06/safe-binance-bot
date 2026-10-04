"""Binance TR (binance.tr) Spot API istemcisi.

Binance TR, Binance Global'den ayrı bir borsadır ve kendi "open/v1" API'sini kullanır:
- Semboller alt çizgili yazılır (BTC_USDT). Bot içinde Binance biçimi (BTCUSDT) kullanılır,
  dönüşüm bu sınıfta yapılır.
- Yanıtlar {"code": 0, "msg": "...", "data": ...} zarfı içindedir; code != 0 hatadır.
- Emir yönü ve tipi sayısal kodlarla gönderilir (side: 0=BUY, 1=SELL; type: 2=MARKET,
  4=STOP_LOSS_LIMIT), emir durumu da sayısaldır (0=NEW, 1=PARTIALLY_FILLED, 2=FILLED ...).
- "type 1" sembollerin emir defteri Binance ile ortaktır; bu sembollerin piyasa verisi
  (fiyat, mum, defter) Binance'in herkese açık Spot API'sinden okunur. Bot yalnızca bu
  sembollerle işlem yapar.

Bu modülde para çekme (withdraw), margin veya futures çağrısı YOKTUR.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import requests

from binance_client.client import BinanceAPIError, BinanceSpotClient
from binance_client.filters import SymbolFilters, fmt_decimal, parse_exchange_info

logger = logging.getLogger(__name__)

SIDE_CODES = {"BUY": 0, "SELL": 1}
TYPE_CODES = {"LIMIT": 1, "MARKET": 2, "STOP_LOSS": 3, "STOP_LOSS_LIMIT": 4,
              "TAKE_PROFIT": 5, "TAKE_PROFIT_LIMIT": 6, "LIMIT_MAKER": 7}
STATUS_NAMES = {-2: "NEW", 0: "NEW", 1: "PARTIALLY_FILLED", 2: "FILLED", 3: "CANCELED",
                4: "PENDING_CANCEL", 5: "REJECTED", 6: "EXPIRED"}


def to_tr_symbol(symbol: str, base: str) -> str:
    return f"{base}_{symbol[len(base):]}"


def normalize_order(order: dict, symbol: str | None = None) -> dict:
    """TR emir yanıtını Binance Global biçimine çevirir."""
    raw_status = order.get("status")
    try:
        status = STATUS_NAMES.get(int(raw_status), str(raw_status))
    except (TypeError, ValueError):
        status = str(raw_status)
    executed = order.get("executedQty", "0") or "0"
    quote = order.get("executedQuoteQty", order.get("cummulativeQuoteQty", "0")) or "0"
    return {
        "orderId": order.get("orderId"),
        "symbol": symbol or str(order.get("symbol", "")).replace("_", ""),
        "status": status,
        "origQty": order.get("origQty", "0"),
        "executedQty": str(executed),
        "cummulativeQuoteQty": str(quote),
        "price": order.get("price", "0"),
        "stopPrice": order.get("stopPrice", "0"),
    }


class BinanceTRClient:
    """BinanceSpotClient ile aynı arayüzü sunan Binance TR istemcisi."""

    EXCHANGE_INFO_TTL = 3600
    FILL_WAIT_SECONDS = 10

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://www.binance.tr",
        market_data: BinanceSpotClient | None = None,
        timeout: int = 15,
        recv_window: int = 5000,
        session: requests.Session | None = None,
    ):
        self._key = api_key
        self._secret = api_secret
        self._has_keys = bool(api_key and api_secret)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.recv_window = recv_window
        self.market = market_data or BinanceSpotClient(base_url="https://api.binance.com", timeout=timeout)
        self._session = session or requests.Session()
        self._filters: dict[str, SymbolFilters] = {}
        self._symbols_raw: list[dict] = []
        self._tr_symbols: dict[str, str] = {}
        self._loaded_at = 0.0
        self._fee_rate: float | None = None

    @property
    def has_keys(self) -> bool:
        return self._has_keys

    # --- HTTP ---
    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False,
                 retries: int = 2) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if signed:
            if not self._has_keys:
                raise BinanceAPIError("Bu işlem için Binance TR API anahtarı gerekli (.env)")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self.recv_window
            query = urlencode(params)
            signature = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            query += f"&signature={signature}"
            headers["X-MBX-APIKEY"] = self._key
        else:
            query = urlencode(params)
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                if method == "GET":
                    resp = self._session.get(f"{url}?{query}" if query else url, headers=headers,
                                             timeout=self.timeout)
                else:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    resp = self._session.post(url, data=query, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(1 + attempt)
                continue
            if resp.status_code == 429 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            try:
                body = resp.json()
            except ValueError:
                raise BinanceAPIError(f"Binance TR geçersiz yanıt (HTTP {resp.status_code})")
            code = body.get("code", 0) if isinstance(body, dict) else 0
            if resp.status_code >= 400 or code not in (0, "0", None):
                raise BinanceAPIError(f"Binance TR hata {code}: {body.get('msg', resp.status_code)}",
                                      code=int(code) if str(code).lstrip("-").isdigit() else None)
            return body.get("data") if isinstance(body, dict) else body
        raise BinanceAPIError(f"Binance TR isteği başarısız: {last_exc}") from last_exc

    # --- Semboller ve filtreler ---
    def load_exchange_info(self, force: bool = False) -> dict[str, SymbolFilters]:
        if not force and self._filters and time.time() - self._loaded_at < self.EXCHANGE_INFO_TTL:
            return self._filters
        data = self._request("GET", "/open/v1/common/symbols") or {}
        raw: list[dict] = []
        tr_symbols: dict[str, str] = {}
        for s in data.get("list", []):
            base, quote = s.get("baseAsset"), s.get("quoteAsset")
            if not base or not quote:
                continue
            symbol = f"{base}{quote}"
            tr_symbols[symbol] = s.get("symbol") or f"{base}_{quote}"
            shared_book = str(s.get("type")) == "1"
            enabled = str(s.get("spotTradingEnable", "1")) == "1"
            raw.append({
                "symbol": symbol,
                "baseAsset": base,
                "quoteAsset": quote,
                # Ortak defterde olmayan semboller için piyasa verisi yok: işlem yapılmaz
                "status": "TRADING" if (enabled and shared_book) else "BREAK",
                "isSpotTradingAllowed": enabled,
                "orderTypes": s.get("orderTypes", []),
                "filters": s.get("filters", []),
            })
        self._symbols_raw = raw
        self._tr_symbols = tr_symbols
        self._filters = parse_exchange_info({"symbols": raw})
        self._loaded_at = time.time()
        return self._filters

    def get_filters(self, symbol: str) -> SymbolFilters:
        filters = self.load_exchange_info()
        if symbol not in filters:
            raise BinanceAPIError(f"{symbol} Binance TR'de bulunamadı")
        return filters[symbol]

    def symbols_raw(self) -> list[dict]:
        self.load_exchange_info()
        return self._symbols_raw

    def tr_symbol(self, symbol: str) -> str:
        self.load_exchange_info()
        if symbol in self._tr_symbols:
            return self._tr_symbols[symbol]
        return to_tr_symbol(symbol, self.get_filters(symbol).base_asset)

    # --- Piyasa verisi (Binance ortak defter) ---
    def ping(self) -> bool:
        try:
            self._request("GET", "/open/v1/common/time", retries=0)
            return True
        except BinanceAPIError:
            return False

    def ticker_24h_all(self) -> list[dict]:
        return self.market.ticker_24h_all()

    def book_tickers(self) -> dict[str, dict]:
        return self.market.book_tickers()

    def book_ticker(self, symbol: str) -> dict:
        return self.market.book_ticker(symbol)

    def price(self, symbol: str) -> float:
        return self.market.price(symbol)

    def prices(self) -> dict[str, float]:
        return self.market.prices()

    def klines(self, symbol: str, interval: str, limit: int = 300) -> list[list]:
        return self.market.klines(symbol, interval, limit=limit)

    # --- Hesap ---
    def account(self) -> dict:
        return self._request("GET", "/open/v1/account/spot", signed=True) or {}

    def balances(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for b in self.account().get("accountAssets", []):
            free, locked = float(b.get("free", 0) or 0), float(b.get("locked", 0) or 0)
            if free > 0 or locked > 0:
                out[b["asset"]] = {"free": free, "locked": locked}
        return out

    def taker_fee_rate(self, default: float) -> float:
        try:
            rate = float(self.account().get("takerCommission", default))
        except (BinanceAPIError, TypeError, ValueError):
            return default
        # Bazı yanıtlarda oran yerine baz puan (10 = %0.1) dönebilir
        if rate >= 1:
            rate = rate / 10000
        self._fee_rate = rate if rate >= 0 else default
        return self._fee_rate

    def api_permissions(self) -> dict:
        """Binance TR API anahtar yetkilerini sorgulama uç noktası sunmaz.

        Hesabın erişilebilir olduğunu doğrular; para çekme yetkisinin kapalı olduğunu
        kullanıcı Binance TR panelinden kendisi kontrol etmelidir.
        """
        acc = self.account()
        return {
            "enableSpotAndMarginTrading": str(acc.get("canTrade", 1)) == "1",
            "permissions_verifiable": False,
        }

    # --- Emirler (yalnızca LIVE) ---
    def _order_detail(self, order_id: int | str) -> dict:
        data = self._request("GET", "/open/v1/orders/detail", {"orderId": order_id}, signed=True)
        if isinstance(data, dict) and "list" in data:
            data = (data.get("list") or [{}])[0]
        return data or {}

    def _wait_fill(self, order: dict) -> dict:
        """Piyasa emri yanıtı hemen FILLED dönmeyebilir; kısa süre durumunu sorgular."""
        deadline = time.time() + self.FILL_WAIT_SECONDS
        while str(order.get("status")) not in ("2", "3", "5", "6") and time.time() < deadline:
            time.sleep(0.5)
            order = self._order_detail(order["orderId"]) or order
        return order

    def _order_commission(self, order_id: int | str, symbol: str) -> list[dict]:
        """Emrin gerçekleşen işlemlerinden komisyon bilgisini okur (yoksa boş liste)."""
        try:
            data = self._request("GET", "/open/v1/orders/trades",
                                 {"orderId": order_id, "symbol": self.tr_symbol(symbol)}, signed=True)
        except BinanceAPIError as exc:
            logger.warning("Binance TR işlem komisyonu okunamadı: %s", exc)
            return []
        trades = data.get("list", []) if isinstance(data, dict) else (data or [])
        fills = []
        for t in trades:
            if str(t.get("orderId", order_id)) != str(order_id):
                continue
            fills.append({"price": str(t.get("price", "0")), "qty": str(t.get("qty", "0")),
                          "commission": str(t.get("commission", "0")),
                          "commissionAsset": t.get("commissionAsset", "")})
        return fills

    def _market(self, symbol: str, side: str, quantity: Decimal) -> dict:
        order = self._request("POST", "/open/v1/orders", {
            "symbol": self.tr_symbol(symbol), "side": SIDE_CODES[side],
            "type": TYPE_CODES["MARKET"], "quantity": fmt_decimal(quantity),
        }, signed=True, retries=0)
        try:
            order = self._wait_fill(order)
        except BinanceAPIError as exc:
            # Emir borsaya ULAŞTI; yalnızca durum sorgusu başarısız. Pozisyonun kayıtsız (stopsuz)
            # kalmaması için piyasa emrinin tamamen dolduğu varsayılır; motor miktarı gerçek
            # bakiyeyle ayrıca doğrular.
            logger.error("Binance TR emir durumu okunamadı (emir gönderildi): %s", exc)
            order = dict(order, status=2)
        norm = normalize_order(order, symbol)
        executed = float(norm["executedQty"])
        quote = float(norm["cummulativeQuoteQty"])
        if executed <= 0 and str(order.get("status")) == "2":
            # Dolum bilgisi yoksa istenen miktar ve güncel defter fiyatı kullanılır
            book = self.market.book_ticker(symbol)
            ref = float(book["askPrice"] if side == "BUY" else book["bidPrice"])
            executed = float(quantity)
            quote = executed * ref
            norm["executedQty"], norm["cummulativeQuoteQty"] = str(executed), str(quote)
            norm["status"] = "FILLED"
        fills = self._order_commission(norm["orderId"], symbol) if executed > 0 else []
        if not fills and executed > 0:
            # Komisyon detayı alınamazsa taker oranıyla tahmin edilir: alışta baz varlıktan,
            # satışta USDT'den kesildiği varsayılır (muhafazakâr miktar hesabı için).
            rate = self._fee_rate if self._fee_rate is not None else 0.001
            price = quote / executed
            commission = executed * rate if side == "BUY" else quote * rate
            asset = self.get_filters(symbol).base_asset if side == "BUY" else "USDT"
            fills = [{"price": str(price), "qty": str(executed), "commission": str(commission),
                      "commissionAsset": asset}]
        norm["fills"] = fills
        return norm

    def market_buy(self, symbol: str, quantity: Decimal) -> dict:
        return self._market(symbol, "BUY", quantity)

    def market_sell(self, symbol: str, quantity: Decimal) -> dict:
        return self._market(symbol, "SELL", quantity)

    def stop_loss_limit_sell(self, symbol: str, quantity: Decimal, stop_price: Decimal,
                             limit_price: Decimal) -> dict:
        order = self._request("POST", "/open/v1/orders", {
            "symbol": self.tr_symbol(symbol), "side": SIDE_CODES["SELL"],
            "type": TYPE_CODES["STOP_LOSS_LIMIT"], "quantity": fmt_decimal(quantity),
            "price": fmt_decimal(limit_price), "stopPrice": fmt_decimal(stop_price),
        }, signed=True, retries=0)
        return normalize_order(order, symbol)

    def cancel_order(self, symbol: str, order_id: int | str) -> dict:
        order = self._request("POST", "/open/v1/orders/cancel", {"orderId": order_id},
                              signed=True, retries=0)
        return normalize_order(order, symbol)

    def get_order(self, symbol: str, order_id: int | str) -> dict:
        order = self._order_detail(order_id)
        if not order:
            raise BinanceAPIError(f"Binance TR emri bulunamadı: {order_id}")
        return normalize_order(order, symbol)

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"type": 1, "limit": 500}
        if symbol:
            params["symbol"] = self.tr_symbol(symbol)
        data = self._request("GET", "/open/v1/orders", params, signed=True)
        orders = data.get("list", []) if isinstance(data, dict) else (data or [])
        return [normalize_order(o) for o in orders]
