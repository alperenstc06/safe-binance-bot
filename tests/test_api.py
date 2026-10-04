from fastapi.testclient import TestClient

from app.main import create_app
from database.database import Database
from tests.conftest import FakeClient, make_settings


def make_client(**kw):
    s = make_settings(**kw)
    app = create_app(s, client=FakeClient(), db=Database("sqlite:///:memory:"), autostart=False)
    return TestClient(app), app


def test_status_and_panel():
    client, app = make_client()
    with client:
        r = client.get("/api/status")
        assert r.status_code == 200
        data = r.json()
        assert data["mode"] == "DRY_RUN" and data["running"] is False
        assert client.get("/").status_code == 200
        assert "Tüm Pozisyonları Kapat" in client.get("/").text


def test_close_all_requires_confirmation():
    client, app = make_client()
    with client:
        app.state.engine.run_cycle(force_scan=True)
        assert len(app.state.engine.db.open_trades("DRY_RUN")) == 1
        r = client.post("/api/close-all", json={"confirm": "evet"})
        assert r.status_code == 400
        assert len(app.state.engine.db.open_trades("DRY_RUN")) == 1
        r = client.post("/api/close-all", json={"confirm": "TUM POZISYONLARI KAPAT"})
        assert r.status_code == 200
        assert not app.state.engine.db.open_trades("DRY_RUN")
        trades = client.get("/api/trades").json()
        assert trades[0]["exit_reason"] == "MANUAL_CLOSE_ALL"
        assert client.get("/api/signals").json()


def test_start_stop_emergency_endpoints():
    client, app = make_client(loop_interval_seconds=3600)
    with client:
        assert client.post("/api/start").json()["running"] is True
        assert client.post("/api/stop").json()["running"] is False
        assert client.post("/api/emergency-stop").status_code == 200
        assert client.post("/api/start").status_code == 409
        assert client.post("/api/emergency-reset", json={"confirm": "x"}).status_code == 400
        assert client.post("/api/emergency-reset", json={"confirm": "ACIL DURUMU SIFIRLA"}).status_code == 200


def test_panel_token_required_when_set():
    client, app = make_client(panel_token="gizli")
    with client:
        assert client.post("/api/stop").status_code == 401
        assert client.post("/api/stop", headers={"X-Panel-Token": "gizli"}).status_code == 200
