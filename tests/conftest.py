"""Test yardımcıları: ağ bağlantısı olmadan çalışan sahte Binance istemcisi."""

from __future__ import annotations

import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from binance_client.client import BinanceAPIError  # noqa: E402
from binance_client.filters import parse_exchange_info  # noqa: E402
from database.database import Database  # noqa: E402

HOUR_MS = 3_600_000


def make_klines(start: float, n: int = 300, up: float = 0.010, down: float = -0.006,
                qv: float = 1_000_000.0, recent_qv_mult: float = 1.5) -> list[list]:
    """Dalgalı yükselen (up/down dönüşümlü) sentetik mum verisi."""
    out = []
    price = start
    for i in range(n):
        o = price
        c = o * (1 + (up if i % 2 == 0 else down))
        h, l = max(o, c) * 1.002, min(o, c) * 0.998
        vol_q = qv * (recent_qv_mult if i >= n - 24 else 1.0)
        out.append([i * HOUR_MS, f"{o:.8f}", f"{h:.8f}", f"{l:.8f}", f"{c:.8f}", "1000",
                    i * HOUR_MS + HOUR_MS - 1, f"{vol_q:.2f}", 100, "0", "0", "0"])
        price = c
    return out


def symbol_info(symbol: str, base: str, tick: str = "0.01", step: str = "0.0001",
                min_qty: str = "0.0001", min_notional: str = "5", status: str = "TRADING") -> dict:
    return {
        "symbol": symbol, "status": status, "baseAsset": base, "quoteAsset": "USDT",
        "isSpotTradingAllowed": True,
        "orderTypes": ["LIMIT", "MARKET", "STOP_LOSS_LIMIT", "LIMIT_MAKER"],
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": tick, "maxPrice": "1000000", "tickSize": tick},
            {"filterType": "LOT_SIZE", "minQty": min_qty, "maxQty": "9000000", "stepSize": step},
            {"filterType": "NOTIONAL", "minNotional": min_notional, "applyMinToMarket": True,
             "maxNotional": "9000000", "avgPriceMins": 5},
        ],
    }


class FakeClient:
    """BinanceSpotClient arayüzünü taklit eden, tamamen yerel sahte istemci."""

    def __init__(self, has_keys: bool = False):
        self._has_keys = has_keys
        self.infos = [
            symbol_info("BTCUSDT", "BTC", tick="0.01", step="0.00001", min_qty="0.00001"),
            symbol_info("ETHUSDT", "ETH", tick="0.01", step="0.0001"),
            symbol_info("SOLUSDT", "SOL", tick="0.01", step="0.001"),
            symbol_info("USDCUSDT", "USDC", tick="0.0001", step="1"),
            symbol_info("BTCUPUSDT", "BTCUP", tick="0.001", step="0.01"),
            symbol_info("NEWUSDT", "NEW", tick="0.0001", step="0.1"),
            symbol_info("LOWUSDT", "LOW", tick="0.0001", step="0.1"),
            symbol_info("DEADUSDT", "DEAD", status="BREAK"),
        ]
        self.kl = {
            "BTCUSDT": make_klines(30000),
            "ETHUSDT": make_klines(1000),
            "SOLUSDT": make_klines(20, up=0.003, down=-0.009),  # düşüş trendi
            "NEWUSDT": make_klines(1),
            "LOWUSDT": make_klines(1),
            "USDCUSDT": make_klines(1, up=0.0, down=0.0),
            "BTCUPUSDT": make_klines(5),
            # Binance TR'nin TL pariteleri (ortak defter) ve kur
            "BTCTRY": make_klines(30000 * 40),
            "ETHTRY": make_klines(1000 * 40),
            "SOLTRY": make_klines(20 * 40, up=0.003, down=-0.009),
            "USDTTRY": make_klines(40, up=0.0, down=0.0),
        }
        self.daily_len = {s: 60 for s in self.kl}
        self.daily_len["NEWUSDT"] = 5
        self.quote_volume = {"BTCUSDT": 900e6, "ETHUSDT": 400e6, "SOLUSDT": 150e6,
                             "NEWUSDT": 80e6, "LOWUSDT": 1e6, "USDCUSDT": 500e6, "BTCUPUSDT": 50e6,
                             "BTCTRY": 900e6 * 40, "ETHTRY": 400e6 * 40, "SOLTRY": 150e6 * 40,
                             "USDTTRY": 500e6 * 40}
        self.change_pct = {s: 1.5 for s in self.kl}
        self.spread = {s: 0.0001 for s in self.kl}
        self.override_price: dict[str, float] = {}
        self.balances_data: dict[str, dict[str, float]] = {"USDT": {"free": 1000.0, "locked": 0.0}}
        self.permissions = {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}
        self.orders: dict[int, dict] = {}
        self.next_id = 1
        self.calls: list[tuple] = []
        self.fail_stop = False

    @property
    def has_keys(self) -> bool:
        return self._has_keys

    def last_price(self, symbol: str) -> float:
        if symbol in self.override_price:
            return self.override_price[symbol]
        return float(self.kl[symbol][-1][4])

    # piyasa
    def load_exchange_info(self, force: bool = False):
        return parse_exchange_info({"symbols": self.infos})

    def get_filters(self, symbol: str):
        f = self.load_exchange_info()
        if symbol not in f:
            raise BinanceAPIError(f"{symbol} yok")
        return f[symbol]

    def symbols_raw(self):
        return self.infos

    def ticker_24h_all(self):
        return [{"symbol": s, "quoteVolume": str(self.quote_volume[s]),
                 "priceChangePercent": str(self.change_pct[s]), "lastPrice": str(self.last_price(s))}
                for s in self.kl]

    def book_ticker(self, symbol: str):
        p = self.last_price(symbol)
        half = p * self.spread[symbol] / 2
        return {"symbol": symbol, "bidPrice": f"{p - half:.8f}", "askPrice": f"{p + half:.8f}"}

    def book_tickers(self):
        return {s: self.book_ticker(s) for s in self.kl}

    def price(self, symbol: str) -> float:
        if symbol not in self.kl:
            raise BinanceAPIError("fiyat yok")
        return self.last_price(symbol)

    def prices(self):
        return {s: self.last_price(s) for s in self.kl}

    def klines(self, symbol: str, interval: str, limit: int = 300):
        if interval == "1d":
            return [[0, "1", "1", "1", "1", "1", 0, "1"]] * min(self.daily_len.get(symbol, 60), limit)
        return self.kl[symbol][-limit:]

    # hesap
    def balances(self):
        return {k: dict(v) for k, v in self.balances_data.items() if v["free"] + v["locked"] > 0}

    def taker_fee_rate(self, default: float) -> float:
        return default

    def api_permissions(self):
        return dict(self.permissions)

    # emirler (LIVE testleri için)
    def _bal(self, asset):
        return self.balances_data.setdefault(asset, {"free": 0.0, "locked": 0.0})

    def market_buy(self, symbol: str, quantity: Decimal):
        self.calls.append(("BUY", symbol, quantity))
        qty = float(quantity)
        price = float(self.book_ticker(symbol)["askPrice"])
        base = symbol[:-4]
        fee_base = qty * 0.001
        self._bal("USDT")["free"] -= qty * price
        self._bal(base)["free"] += qty - fee_base
        oid = self.next_id
        self.next_id += 1
        return {"orderId": oid, "status": "FILLED", "executedQty": str(qty),
                "cummulativeQuoteQty": str(qty * price),
                "fills": [{"price": str(price), "qty": str(qty), "commission": str(fee_base),
                           "commissionAsset": base}]}

    def market_sell(self, symbol: str, quantity: Decimal):
        self.calls.append(("SELL", symbol, quantity))
        qty = float(quantity)
        price = float(self.book_ticker(symbol)["bidPrice"])
        base = symbol[:-4]
        quote = qty * price
        self._bal(base)["free"] -= qty
        self._bal("USDT")["free"] += quote * 0.999
        oid = self.next_id
        self.next_id += 1
        return {"orderId": oid, "status": "FILLED", "executedQty": str(qty),
                "cummulativeQuoteQty": str(quote),
                "fills": [{"price": str(price), "qty": str(qty), "commission": str(quote * 0.001),
                           "commissionAsset": "USDT"}]}

    def stop_loss_limit_sell(self, symbol, quantity, stop_price, limit_price):
        if self.fail_stop:
            raise BinanceAPIError("stop reddedildi")
        self.calls.append(("STOP", symbol, quantity, stop_price, limit_price))
        base = symbol[:-4]
        q = float(quantity)
        self._bal(base)["free"] -= q
        self._bal(base)["locked"] += q
        oid = self.next_id
        self.next_id += 1
        self.orders[oid] = {"orderId": oid, "symbol": symbol, "status": "NEW", "origQty": str(q),
                            "executedQty": "0", "cummulativeQuoteQty": "0",
                            "stopPrice": str(stop_price), "price": str(limit_price)}
        return self.orders[oid]

    def fill_stop(self, oid: int, price: float):
        o = self.orders[oid]
        q = float(o["origQty"])
        base = o["symbol"][:-4]
        self._bal(base)["locked"] -= q
        self._bal("USDT")["free"] += q * price * 0.999
        o.update(status="FILLED", executedQty=str(q), cummulativeQuoteQty=str(q * price))

    def cancel_order(self, symbol, order_id):
        self.calls.append(("CANCEL", symbol, order_id))
        o = self.orders.get(int(order_id))
        if o is None or o["status"] != "NEW":
            raise BinanceAPIError("Unknown order sent.", code=-2011)
        base = symbol[:-4]
        q = float(o["origQty"])
        self._bal(base)["locked"] -= q
        self._bal(base)["free"] += q
        o["status"] = "CANCELED"
        return dict(o)

    def get_order(self, symbol, order_id):
        o = self.orders.get(int(order_id))
        if o is None:
            raise BinanceAPIError("Order does not exist.", code=-2013)
        return dict(o)

    def open_orders(self, symbol=None):
        return [o for o in self.orders.values() if o["status"] == "NEW"]


def make_settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        database_url="sqlite:///:memory:",
        trading_mode="DRY_RUN",
        binance_api_key="",
        binance_api_secret="",
        auto_start=False,
        loop_interval_seconds=1,
        scan_interval_seconds=0,
        log_dir="logs",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings():
    return make_settings()


@pytest.fixture
def db():
    return Database("sqlite:///:memory:")


@pytest.fixture
def fake():
    return FakeClient()
