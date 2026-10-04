"""Telegram bildirim/komut testleri (ağ yok)."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import create_app
from bot.notifier import TelegramNotifier, format_positions, format_status
from database.database import Database
from tests.conftest import FakeClient, make_settings


class Resp:
    status_code = 200

    def json(self):
        return {"ok": True, "result": []}


class FakeTG:
    def __init__(self):
        self.sent = []

    def post(self, url, data=None, timeout=None):
        self.sent.append((url, data))
        return Resp()

    def get(self, url, params=None, timeout=None):
        return Resp()


def make_notifier(chat_id="42"):
    session = FakeTG()
    n = TelegramNotifier("123:ABC", chat_id, session=session)
    return n, session


def drain(n):
    out = []
    while not n._queue.empty():
        out.append(n._queue.get_nowait())
    return out


def test_on_log_filters_categories_and_dedupes_errors():
    n, _ = make_notifier()
    n.on_log("TRADE", "ALIŞ ETHTRY")
    n.on_log("REGIME", "Piyasa rejimi: BULL")  # gönderilmez
    n.on_log("NO_TRADE", "Uygun fırsat yok")  # gönderilmez
    n.on_log("STOP", "ETHTRY stop yükseltildi 1 -> 2 (başa baş)")
    n.on_log("SAFETY", "BINANCE_TR API anahtar yetkileri otomatik doğrulanamıyor.")  # gönderilmez
    n.on_log("ERROR", "bağlantı hatası", level="ERROR")
    n.on_log("ERROR", "bağlantı hatası", level="ERROR")  # tekrar: gönderilmez
    msgs = drain(n)
    assert len(msgs) == 3
    assert "ALIŞ" in msgs[0] and "başa baş" in msgs[1] and "bağlantı" in msgs[2]


def test_unauthorized_chat_ignored_and_chat_id_discovery():
    n, session = make_notifier("42")
    assert n.handle_update({"update_id": 1, "message": {"chat": {"id": 99}, "text": "/acil EVET"}}) is None
    assert not session.sent
    n2, s2 = make_notifier("")
    reply = n2.handle_update({"update_id": 1, "message": {"chat": {"id": 77}, "text": "merhaba"}})
    assert "77" in reply and s2.sent and s2.sent[0][1]["chat_id"] == "77"


def test_emergency_command_requires_confirmation():
    n, session = make_notifier("42")
    called = []
    n.emergency_handler = lambda: called.append(True)
    n.status_provider = lambda: {}
    r1 = n.handle_update({"update_id": 1, "message": {"chat": {"id": 42}, "text": "/acil"}})
    assert "EVET" in r1 and not called
    n.handle_update({"update_id": 2, "message": {"chat": {"id": 42}, "text": "/acil evet"}})
    assert called == [True]


def test_status_and_position_formatting():
    s = {"running": True, "mode": "LIVE", "exchange": "BINANCE_TR", "regime": "BULL",
         "equity_usdt": 333.3, "quote_free": 3232.5, "quote_asset": "TRY", "daily_pnl": 8.9,
         "total_pnl": 8.9, "trades_today": 1, "consecutive_losses": 0, "no_trade_reason": "x",
         "positions": [{"symbol": "PUMPTRY", "quantity": 5279.0, "entry_price": 0.3092,
                        "current_price": 0.312, "stop_price": 0.2976, "trailing_active": False,
                        "breakeven_active": False, "pnl_usdt": 8.91, "pnl_pct": 0.55,
                        "partial_taken": True}]}
    text = format_status(s)
    assert "ÇALIŞIYOR" in text and "TRY" in text and "333,30" in text
    pos = format_positions(s)
    assert "PUMPTRY" in pos and "yarısı satıldı" in pos and "8,91" in pos
    assert format_positions({"positions": []}) == "Açık pozisyon yok."


def test_database_listener_receives_decisions():
    db = Database("sqlite:///:memory:")
    got = []
    db.listener = lambda c, m, l: got.append((c, m, l))
    db.log_decision("TRADE", "ALIŞ X")
    assert got == [("TRADE", "ALIŞ X", "INFO")]
    db.listener = lambda *a: 1 / 0  # dinleyici hatası kaydı engellemez
    db.log_decision("TRADE", "ALIŞ Y")
    assert db.recent_logs(1)[0].message == "ALIŞ Y"


def test_panel_on_network_requires_token():
    with pytest.raises(ValidationError):
        make_settings(panel_host="0.0.0.0")
    assert make_settings(panel_host="0.0.0.0", panel_token="gizli").panel_host == "0.0.0.0"
    assert make_settings(panel_host="0.0.0.0", panel_insecure_ok=True)


def test_token_protects_read_endpoints_too():
    s = make_settings(panel_token="gizli")
    app = create_app(s, client=FakeClient(), db=Database("sqlite:///:memory:"), autostart=False)
    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers={"X-Panel-Token": "gizli"}).status_code == 200
        assert c.get("/api/health").status_code == 200
        assert c.get("/").status_code == 200


def test_app_wires_telegram_notifier():
    s = make_settings(telegram_bot_token="123:ABC", telegram_chat_id="42",
                      telegram_commands_enabled=False)
    app = create_app(s, client=FakeClient(), db=Database("sqlite:///:memory:"), autostart=False)
    n = app.state.notifier
    assert n is not None and n.status_provider is not None
    n.notify = lambda text: sent.append(text)
    sent = []
    app.state.engine.db.log_decision("TRADE", "ALIŞ TEST")
    assert sent == ["💱 ALIŞ TEST"]
