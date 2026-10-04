"""Binance TR istemcisi testleri (ağ yok, sahte HTTP oturumu)."""

import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qsl, urlparse

import pytest

from binance_client.client import BinanceAPIError
from binance_client.tr_client import BinanceTRClient, normalize_order
from bot.engine import BotEngine
from database.database import Database
from tests.conftest import FakeClient, make_settings

SECRET = "trsecret"


def tr_symbol(base, quote="USDT", type_=1, step="0.0001", tick="0.01", enabled=1):
    return {
        "type": type_, "symbol": f"{base}_{quote}", "baseAsset": base, "quoteAsset": quote,
        "spotTradingEnable": enabled,
        "orderTypes": ["LIMIT", "MARKET", "STOP_LOSS_LIMIT", "LIMIT_MAKER"],
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": tick, "maxPrice": "1000000", "tickSize": tick},
            {"filterType": "LOT_SIZE", "minQty": step, "maxQty": "9000000", "stepSize": step},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5", "applyToMarket": True},
        ],
    }


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body


class FakeSession:
    """Binance TR open/v1 API'sini taklit eder."""

    def __init__(self, market: FakeClient):
        self.market = market
        self.calls = []
        self.balances = {"USDT": {"free": "99", "locked": "0"}}
        self.orders = {}
        self.next_id = 100
        self.trades_available = True
        self.fill_immediately = False
        self.error = None

    def _env(self, data, code=0, msg="Success"):
        return Resp({"code": code, "msg": msg, "data": data, "timestamp": 1})

    def _check_sig(self, query, headers):
        params = dict(parse_qsl(query))
        sig = params.pop("signature")
        unsigned = query.rsplit("&signature=", 1)[0]
        assert sig == hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
        assert headers["X-MBX-APIKEY"] == "trkey"
        assert "timestamp" in params
        return params

    def get(self, url, headers=None, timeout=None):
        parsed = urlparse(url)
        self.calls.append(("GET", parsed.path, parsed.query))
        if self.error:
            return self._env(None, code=self.error[0], msg=self.error[1])
        if parsed.path == "/open/v1/common/symbols":
            return self._env({"list": [tr_symbol("BTC", step="0.00001"), tr_symbol("ETH"),
                                       tr_symbol("SOL", step="0.001"),
                                       tr_symbol("AVAX", quote="TRY", type_=0),
                                       tr_symbol("LOCAL", type_=0)]})
        params = self._check_sig(parsed.query, headers)
        if parsed.path == "/open/v1/account/spot":
            assets = [{"asset": a, **v} for a, v in self.balances.items()]
            return self._env({"takerCommission": "0.00100000", "canTrade": 1, "accountAssets": assets})
        if parsed.path == "/open/v1/orders/detail":
            o = self.orders[int(params["orderId"])]
            o["status"] = 2 if o["status"] == 0 and o["type"] == 2 else o["status"]
            return self._env(dict(o))
        if parsed.path == "/open/v1/orders/trades":
            o = self.orders[int(params["orderId"])]
            if not self.trades_available:
                return self._env({"list": []})
            base = o["symbol"].split("_")[0]
            asset = base if o["side"] == 0 else "USDT"
            comm = float(o["executedQty"]) * 0.001 if o["side"] == 0 else float(o["executedQuoteQty"]) * 0.001
            return self._env({"list": [{"orderId": o["orderId"], "price": o["executedPrice"],
                                        "qty": o["executedQty"], "commission": str(comm),
                                        "commissionAsset": asset}]})
        raise AssertionError(parsed.path)

    def post(self, url, data=None, headers=None, timeout=None):
        path = urlparse(url).path
        self.calls.append(("POST", path, data))
        params = self._check_sig(data, headers)
        if path == "/open/v1/orders":
            oid = self.next_id
            self.next_id += 1
            sym = params["symbol"].replace("_", "")
            qty = float(params["quantity"])
            if params["type"] == "2":
                book = self.market.book_ticker(sym)
                price = float(book["askPrice"] if params["side"] == "0" else book["bidPrice"])
                o = {"orderId": oid, "symbol": params["symbol"], "side": int(params["side"]), "type": 2,
                     "origQty": params["quantity"], "executedQty": params["quantity"],
                     "executedPrice": str(price), "executedQuoteQty": str(qty * price),
                     "status": 2 if self.fill_immediately else 0}
            else:
                o = {"orderId": oid, "symbol": params["symbol"], "side": int(params["side"]),
                     "type": int(params["type"]), "origQty": params["quantity"], "executedQty": "0",
                     "executedPrice": "0", "executedQuoteQty": "0", "price": params["price"],
                     "stopPrice": params["stopPrice"], "status": 0}
            self.orders[oid] = o
            return self._env(dict(o))
        if path == "/open/v1/orders/cancel":
            o = self.orders[int(params["orderId"])]
            o["status"] = 3
            return self._env(dict(o))
        raise AssertionError(path)


def make_tr(market=None, keys=True):
    market = market or FakeClient()
    session = FakeSession(market)
    client = BinanceTRClient("trkey" if keys else "", SECRET if keys else "", market_data=market,
                             session=session)
    client.FILL_WAIT_SECONDS = 2
    return client, session, market


def test_symbols_converted_and_only_shared_book_tradable():
    client, _, _ = make_tr()
    filters = client.load_exchange_info()
    assert {"BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXTRY", "LOCALUSDT"} <= set(filters)
    raw = {s["symbol"]: s for s in client.symbols_raw()}
    assert raw["BTCUSDT"]["status"] == "TRADING"
    assert raw["LOCALUSDT"]["status"] == "BREAK"  # ortak defterde değil -> işlem yok
    assert client.tr_symbol("BTCUSDT") == "BTC_USDT"
    assert filters["BTCUSDT"].step_size == Decimal("0.00001")


def test_signed_request_and_balances():
    client, session, _ = make_tr()
    bals = client.balances()
    assert bals == {"USDT": {"free": 99.0, "locked": 0.0}}
    assert client.taker_fee_rate(0.002) == pytest.approx(0.001)
    perms = client.api_permissions()
    assert perms["permissions_verifiable"] is False


def test_error_envelope_raises():
    client, session, _ = make_tr()
    session.error = (-1022, "Signature for this request is not valid.")
    with pytest.raises(BinanceAPIError) as exc:
        client.balances()
    assert exc.value.code == -1022 and "Signature" in str(exc.value)


def test_signed_call_requires_keys():
    client, _, _ = make_tr(keys=False)
    assert not client.has_keys
    with pytest.raises(BinanceAPIError):
        client.balances()


def test_market_buy_waits_for_fill_and_reads_commission():
    client, session, market = make_tr()
    resp = client.market_buy("ETHUSDT", Decimal("0.05"))
    assert resp["status"] == "FILLED"
    assert float(resp["executedQty"]) == pytest.approx(0.05)
    assert resp["fills"][0]["commissionAsset"] == "ETH"
    post = [c for c in session.calls if c[0] == "POST"][0]
    params = dict(parse_qsl(post[2]))
    assert params["symbol"] == "ETH_USDT" and params["side"] == "0" and params["type"] == "2"


def test_market_sell_commission_estimated_when_trades_missing():
    client, session, market = make_tr()
    session.trades_available = False
    session.fill_immediately = True
    client.taker_fee_rate(0.001)
    resp = client.market_sell("ETHUSDT", Decimal("0.05"))
    fill = resp["fills"][0]
    assert fill["commissionAsset"] == "USDT"
    assert float(fill["commission"]) == pytest.approx(float(resp["cummulativeQuoteQty"]) * 0.001)


def test_stop_order_cancel_and_status_normalized():
    client, session, _ = make_tr()
    o = client.stop_loss_limit_sell("ETHUSDT", Decimal("0.05"), Decimal("900"), Decimal("895.5"))
    assert o["status"] == "NEW"
    params = dict(parse_qsl([c for c in session.calls if c[0] == "POST"][-1][2]))
    assert params["type"] == "4" and params["stopPrice"] == "900" and params["price"] == "895.5"
    assert client.get_order("ETHUSDT", o["orderId"])["status"] == "NEW"
    assert client.cancel_order("ETHUSDT", o["orderId"])["status"] == "CANCELED"


def test_normalize_order_status_codes():
    assert normalize_order({"status": 2, "executedQty": "1", "executedQuoteQty": "10"})["status"] == "FILLED"
    assert normalize_order({"status": "1"})["status"] == "PARTIALLY_FILLED"
    assert normalize_order({"status": 5})["status"] == "REJECTED"


# --- Motor entegrasyonu ---

def tr_settings(**kw):
    base = dict(trading_exchange="TR", binance_tr_api_key="trkey", binance_tr_api_secret=SECRET)
    base.update(kw)
    return make_settings(**base)


def test_engine_dry_run_on_tr_uses_separate_book_and_shows_both_accounts():
    tr, session, market = make_tr()
    global_client = FakeClient(has_keys=True)
    global_client.balances_data = {"USDT": {"free": 2.5, "locked": 0.0}}
    db = Database("sqlite:///:memory:")
    engine = BotEngine(tr_settings(), tr, db, accounts={"BINANCE_GLOBAL": global_client})
    assert engine.book == "DRY_RUN@BINANCE_TR"
    engine.run_cycle(force_scan=True)
    trades = db.open_trades("DRY_RUN@BINANCE_TR")
    assert len(trades) == 1 and trades[0].symbol in ("BTCUSDT", "ETHUSDT")
    assert not db.open_trades("DRY_RUN")
    assert not [c for c in session.calls if c[0] == "POST"]  # DRY_RUN: emir yok
    st = engine.status()
    assert st["exchange"] == "BINANCE_TR"
    views = {a["exchange"]: a for a in st["accounts"]}
    assert views["BINANCE_TR"]["trading"] and views["BINANCE_TR"]["portfolio"]["equity_usdt"] == pytest.approx(99)
    assert not views["BINANCE_GLOBAL"]["trading"]
    assert views["BINANCE_GLOBAL"]["portfolio"]["equity_usdt"] == pytest.approx(2.5)
    # Global'in kağıt bakiyesi etkilenmedi
    assert db.get_state("paper_usdt") is None or db.get_state("paper_usdt") == 1000


def test_engine_live_on_tr_places_exchange_stop():
    market = FakeClient()
    tr, session, _ = make_tr(market)
    session.balances = {"USDT": {"free": "1000", "locked": "0"}}
    db = Database("sqlite:///:memory:")
    engine = BotEngine(tr_settings(trading_mode="LIVE"), tr, db)
    engine.safety_check()
    assert any("doğrulanamıyor" in l.message for l in db.recent_logs(10))
    engine.run_cycle(force_scan=True)
    t = db.open_trades("LIVE@BINANCE_TR")[0]
    assert t.stop_order_id is not None
    posts = [dict(parse_qsl(c[2])) for c in session.calls if c[0] == "POST"]
    assert posts[0]["type"] == "2" and posts[1]["type"] == "4"


def test_config_tr_live_requires_tr_keys():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        make_settings(trading_mode="LIVE", trading_exchange="BINANCE_TR",
                      binance_api_key="k", binance_api_secret="s")
    s = tr_settings(trading_mode="LIVE")
    assert s.is_tr and s.has_trading_keys
    with pytest.raises(ValidationError):
        make_settings(trading_exchange="KRAKEN")


def test_market_order_tracked_even_if_status_query_fails():
    client, session, market = make_tr()
    real_get = session.get

    def failing_get(url, headers=None, timeout=None):
        if "/open/v1/orders/" in url:
            return Resp({"code": -1, "msg": "internal error", "data": None})
        return real_get(url, headers=headers, timeout=timeout)
    session.get = failing_get
    resp = client.market_buy("ETHUSDT", Decimal("0.05"))
    assert resp["status"] == "FILLED"
    assert float(resp["executedQty"]) == pytest.approx(0.05)
    assert float(resp["cummulativeQuoteQty"]) > 0
