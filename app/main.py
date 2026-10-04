"""Uygulama giriş noktası: FastAPI paneli + bot motoru.

Çalıştırma:  python -m app.main
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from api.routes import router
from app.config import Settings, get_settings
from binance_client.client import build_clients
from bot.engine import BotEngine
from database.database import Database

STATIC_DIR = Path(__file__).parent / "static"
logger = logging.getLogger("app")


def setup_logging(settings: Settings) -> None:
    root = logging.getLogger()
    if getattr(root, "_bot_configured", False):
        return
    root.setLevel(settings.log_level.upper())
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        os.makedirs(settings.log_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(settings.log_dir, "bot.log"),
                                 maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        root.warning("Log dosyası oluşturulamadı; yalnızca konsola yazılacak")
    root._bot_configured = True  # type: ignore[attr-defined]


def create_app(settings: Settings | None = None, client=None, db: Database | None = None,
               autostart: bool | None = None, accounts: dict | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings)
    if client is None:
        client, built_accounts = build_clients(settings)
        accounts = accounts or built_accounts
    db = db or Database(settings.database_url)
    engine = BotEngine(settings, client, db, accounts=accounts)

    should_autostart = settings.auto_start if autostart is None else autostart
    if settings.is_live and not settings.allow_live_auto_start:
        should_autostart = False

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mode = settings.trading_mode.value
        logger.warning("Bot modu: %s, borsa: %s%s", mode, settings.trading_exchange.value,
                       " (GERÇEK EMİRLER!)" if settings.is_live else
                       " (simülasyon, gerçek emir gönderilmez)")
        if should_autostart:
            try:
                engine.start()
            except Exception as exc:
                logger.error("Bot otomatik başlatılamadı: %s", exc)
                db.log_decision("BOT", f"Otomatik başlatma başarısız: {exc}", level="ERROR")
        yield
        if engine.running:
            engine.stop("Uygulama kapanıyor")

    app = FastAPI(title="Safe Binance Spot Bot", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


def run() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(create_app(settings), host=settings.panel_host, port=settings.panel_port,
                log_level=settings.log_level.lower())


if __name__ == "__main__":
    run()
