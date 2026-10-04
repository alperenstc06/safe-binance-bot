"""Binance Global Spot API istemcisi.

Resmi `binance-connector` (binance.spot.Spot) kütüphanesinin üzerinde ince bir katman.
Yalnızca SPOT uç noktaları kullanılır. Futures, Margin, kaldıraç veya para çekme
(withdraw) çağrısı bu modülde YOKTUR ve eklenmemelidir.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Callable

from binance.error import ClientError, ServerError
from binance.spot import Spot

from binance_client.filters import SymbolFilters, fmt_decimal, parse_exchange_info

logger = logging.getLogger(__name__)


class BinanceAPIError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class BinanceSpotClient:
    """Bot tarafından kullanılan tüm Binance Spot çağrıları."""

    EXCHANGE_INFO_TTL = 3600

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = "https://api.binance.com",
        timeout: int = 15,
        recv_window: int = 5000,
        spot: Spot | None = None,
    ):
        self._has_keys = bool(api_key and api_secret)
        self.recv_window = recv_window
        self._spot = spot or Spot(
            api_key=api_key or None,
            api_secret=api_secret or None,
            base_url=base_url,
            timeout=timeout,
        )
        self._filters: dict[str, SymbolFilters] = {}
        self._symbols_raw: list[dict] = []
        self._filters_loaded_at = 0.0

    @property
    def has_keys(self) -> bool:
        return self._has_keys

    # --- Ortak çağrı sarmalayıcı ---
    def _call(self, fn: Callable[..., Any], *args: Any, retries: int = 2, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return fn(*args, **kwargs)
            except ClientError as exc:  # 4xx: tekrar denemenin anlamı yok (429 hariç)
                if exc.status_code == 429 and attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    last_exc = exc
                    continue
                raise BinanceAPIError(f"Binance hata {exc.error_code}: {exc.error_message}",
                                      code=exc.error_code) from exc
            except ServerError as exc:
                last_exc = exc
            except Exception as exc:  # ağ hataları
                last_exc = exc
            if attempt < retries:
                time.sleep(1 + attempt)
        raise BinanceAPIError(f"Binance isteği başarısız: {last_exc}") from last_exc

    def _require_keys(self) -> None:
        if not self._has_keys:
            raise BinanceAPIError("Bu işlem için API anahtarı gerekli (.env)")

    # --- Piyasa verisi (anahtar gerektirmez) ---
    def ping(self) -> bool:
        try:
            self._call(self._spot.time, retries=0)
            return True
        except BinanceAPIError:
            return False

    def load_exchange_info(self, force: bool = False) -> dict[str, SymbolFilters]:
        if not force and self._filters and time.time() - self._filters_loaded_at < self.EXCHANGE_INFO_TTL:
            return self._filters
        info = self._call(self._spot.exchange_info)
        self._symbols_raw = info.get("symbols", [])
        self._filters = parse_exchange_info(info)
        self._filters_loaded_at = time.time()
        return self._filters

    def get_filters(self, symbol: str) -> SymbolFilters:
        filters = self.load_exchange_info()
        if symbol not in filters:
            raise BinanceAPIError(f"{symbol} exchangeInfo içinde bulunamadı")
        return filters[symbol]

    def symbols_raw(self) -> list[dict]:
        self.load_exchange_info()
        return self._symbols_raw

    def ticker_24h_all(self) -> list[dict]:
        return self._call(self._spot.ticker_24hr)

    def book_tickers(self) -> dict[str, dict]:
        data = self._call(self._spot.book_ticker)
        return {d["symbol"]: d for d in data}

    def book_ticker(self, symbol: str) -> dict:
        return self._call(self._spot.book_ticker, symbol=symbol)

    def price(self, symbol: str) -> float:
        return float(self._call(self._spot.ticker_price, symbol=symbol)["price"])

    def prices(self) -> dict[str, float]:
        data = self._call(self._spot.ticker_price)
        return {d["symbol"]: float(d["price"]) for d in data}

    def klines(self, symbol: str, interval: str, limit: int = 300) -> list[list]:
        return self._call(self._spot.klines, symbol, interval, limit=limit)

    # --- Hesap (anahtar gerektirir, sadece okuma) ---
    def account(self) -> dict:
        self._require_keys()
        return self._call(self._spot.account, omitZeroBalances="true", recvWindow=self.recv_window)

    def balances(self) -> dict[str, dict[str, float]]:
        acc = self.account()
        out: dict[str, dict[str, float]] = {}
        for b in acc.get("balances", []):
            free, locked = float(b["free"]), float(b["locked"])
            if free > 0 or locked > 0:
                out[b["asset"]] = {"free": free, "locked": locked}
        return out

    def taker_fee_rate(self, default: float) -> float:
        try:
            acc = self.account()
        except BinanceAPIError:
            return default
        rates = acc.get("commissionRates") or {}
        try:
            taker = float(rates.get("taker", default))
            return taker if taker >= 0 else default
        except (TypeError, ValueError):
            return default

    def api_permissions(self) -> dict:
        """API anahtarının yetkileri (enableWithdrawals, enableMargin, ...)."""
        self._require_keys()
        return self._call(self._spot.api_key_permissions, recvWindow=self.recv_window)

    # --- Emirler (yalnızca LIVE modda çağrılır) ---
    def market_buy(self, symbol: str, quantity: Decimal) -> dict:
        self._require_keys()
        return self._call(
            self._spot.new_order, symbol=symbol, side="BUY", type="MARKET",
            quantity=fmt_decimal(quantity), newOrderRespType="FULL",
            recvWindow=self.recv_window, retries=0,
        )

    def market_sell(self, symbol: str, quantity: Decimal) -> dict:
        self._require_keys()
        return self._call(
            self._spot.new_order, symbol=symbol, side="SELL", type="MARKET",
            quantity=fmt_decimal(quantity), newOrderRespType="FULL",
            recvWindow=self.recv_window, retries=0,
        )

    def stop_loss_limit_sell(
        self, symbol: str, quantity: Decimal, stop_price: Decimal, limit_price: Decimal
    ) -> dict:
        self._require_keys()
        return self._call(
            self._spot.new_order, symbol=symbol, side="SELL", type="STOP_LOSS_LIMIT",
            timeInForce="GTC", quantity=fmt_decimal(quantity),
            stopPrice=fmt_decimal(stop_price), price=fmt_decimal(limit_price),
            recvWindow=self.recv_window, retries=0,
        )

    def cancel_order(self, symbol: str, order_id: int | str) -> dict:
        self._require_keys()
        return self._call(self._spot.cancel_order, symbol=symbol, orderId=int(order_id),
                          recvWindow=self.recv_window, retries=0)

    def get_order(self, symbol: str, order_id: int | str) -> dict:
        self._require_keys()
        return self._call(self._spot.get_order, symbol=symbol, orderId=int(order_id),
                          recvWindow=self.recv_window)

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        self._require_keys()
        if symbol:
            return self._call(self._spot.get_open_orders, symbol=symbol, recvWindow=self.recv_window)
        return self._call(self._spot.get_open_orders, recvWindow=self.recv_window)


def build_client(settings) -> BinanceSpotClient:
    return BinanceSpotClient(
        api_key=settings.binance_api_key.get_secret_value(),
        api_secret=settings.binance_api_secret.get_secret_value(),
        base_url=settings.effective_base_url,
        timeout=settings.binance_timeout_seconds,
        recv_window=settings.binance_recv_window,
    )


def build_tr_client(settings):
    from binance_client.tr_client import BinanceTRClient

    market = BinanceSpotClient(base_url=settings.binance_tr_market_data_url,
                               timeout=settings.binance_timeout_seconds)
    return BinanceTRClient(
        api_key=settings.binance_tr_api_key.get_secret_value(),
        api_secret=settings.binance_tr_api_secret.get_secret_value(),
        base_url=settings.binance_tr_base_url,
        market_data=market,
        timeout=settings.binance_timeout_seconds,
        recv_window=settings.binance_recv_window,
    )


def build_clients(settings) -> tuple[object, dict[str, object]]:
    """(işlem istemcisi, panelde gösterilecek hesaplar) döndürür."""
    global_client = build_client(settings)
    tr_client = build_tr_client(settings)
    accounts = {"BINANCE_GLOBAL": global_client, "BINANCE_TR": tr_client}
    trading = tr_client if settings.is_tr else global_client
    return trading, accounts
