"""Onarım aracı testleri: önizleme değiştirmez, uygulama öncesi yedek alınır, kayıt silinmez."""

import glob
import os

import pytest

from bot.engine import BotEngine
from database.database import Database
from database.models import Trade, utcnow
from tests.conftest import FakeClient, make_settings
from tools import repair_trades


@pytest.fixture
def incident_db(tmp_path):
    path = str(tmp_path / "bot.db")
    db = Database(f"sqlite:///{path}")
    # Olaydaki hatalı kayıt: 5.051 PUMP alındı, miktar 0.42 yazıldı, %100 zarar ile kapatıldı
    t = db.add_trade(Trade(symbol="ETHUSDT", base_asset="ETH", mode="LIVE", status="CLOSED",
                           quantity=0.42, entry_price=0.3256, entry_quote=1644.6056,
                           initial_stop=0.31, stop_price=0.31, highest_price=0.3256,
                           exit_price=0.3256, exit_quote=0.0, exit_time=utcnow(),
                           exit_reason="STOP_PLACEMENT_FAILED", pnl_usdt=-1644.6056, pnl_pct=-100.0))
    return path, db, t.id


def test_list_flags_suspicious_and_preview_changes_nothing(incident_db, capsys):
    path, db, tid = incident_db
    repair_trades.main(["--db", path, "list"])
    out = capsys.readouterr().out
    assert "ŞÜPHELİ" in out and "tutarsız" in out
    repair_trades.main(["--db", path, "reopen", "--id", str(tid), "--quantity", "5045.9"])
    out = capsys.readouterr().out
    assert "ÖNİZLEME" in out
    assert db.get_trade(tid).status == "CLOSED"
    assert not glob.glob(path + ".yedek-*")


def test_reopen_apply_backs_up_and_bot_resumes_with_stop(incident_db):
    path, db, tid = incident_db
    repair_trades.main(["--db", path, "reopen", "--id", str(tid), "--quantity", "5045.9", "--apply"])
    assert glob.glob(path + ".yedek-*")  # yedek alındı
    t = Database(f"sqlite:///{path}").get_trade(tid)
    assert t.status == "OPEN" and t.quantity == pytest.approx(5045.9)
    assert t.entry_quote == pytest.approx(1644.6056) and t.pnl_usdt is None
    assert "onarım" in t.notes
    # Bot açılınca pozisyonu bakiyeyle doğrular ve borsa stopu koyar (otomatik satış yok)
    fake = FakeClient(has_keys=True)
    fake.override_price["ETHUSDT"] = 0.3256
    fake.balances_data["ETH"] = {"free": 5046.32, "locked": 0.0}  # 5045.9 + eski toz 0.42
    s = make_settings(trading_mode="LIVE", binance_api_key="k", binance_api_secret="s")
    engine = BotEngine(s, fake, Database(f"sqlite:///{path}"))
    engine.sync_positions()
    t2 = engine.db.get_trade(tid)
    assert t2.status == "OPEN" and t2.stop_order_id is not None
    assert not any(c[0] == "SELL" for c in fake.calls)


def test_set_exit_recomputes_pnl_with_backup(incident_db):
    path, db, tid = incident_db
    repair_trades.main(["--db", path, "set-exit", "--id", str(tid), "--exit-quote", "1650.25",
                        "--exit-price", "0.3268", "--apply"])
    t = Database(f"sqlite:///{path}").get_trade(tid)
    assert t.pnl_usdt == pytest.approx(1650.25 - 1644.6056)
    assert t.exit_quote == pytest.approx(1650.25) and glob.glob(path + ".yedek-*")


def test_clear_review(incident_db):
    path, db, tid = incident_db
    db.set_state("review_required", ["x"])
    repair_trades.main(["--db", path, "clear-review", "--apply"])
    assert Database(f"sqlite:///{path}").get_state("review_required") == []
