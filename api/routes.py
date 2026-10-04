"""FastAPI panel uç noktaları."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from bot.engine import BotEngine, SafetyError

CLOSE_ALL_CONFIRM_TEXT = "TUM POZISYONLARI KAPAT"
RESET_EMERGENCY_CONFIRM_TEXT = "ACIL DURUMU SIFIRLA"

router = APIRouter(prefix="/api")


class ConfirmBody(BaseModel):
    confirm: str = ""


def get_engine(request: Request) -> BotEngine:
    return request.app.state.engine


def require_token(request: Request, x_panel_token: str | None = Header(default=None)) -> None:
    """PANEL_TOKEN tanımlıysa tüm kontrol (POST) işlemleri için zorunludur."""
    expected = request.app.state.settings.panel_token.get_secret_value()
    if expected and not hmac.compare_digest(expected, x_panel_token or ""):
        raise HTTPException(status_code=401, detail="Geçersiz panel anahtarı")


def _trade_dict(t) -> dict:
    return {
        "id": t.id, "symbol": t.symbol, "mode": t.mode, "status": t.status,
        "quantity": t.quantity, "entry_price": t.entry_price,
        "entry_time": t.entry_time.isoformat() if t.entry_time else None,
        "exit_price": t.exit_price, "exit_time": t.exit_time.isoformat() if t.exit_time else None,
        "exit_reason": t.exit_reason, "pnl_usdt": t.pnl_usdt, "pnl_pct": t.pnl_pct,
        "fee_usdt": round(t.total_fee_usdt, 6), "score_at_entry": t.score_at_entry,
        "initial_stop": t.initial_stop, "stop_price": t.stop_price,
    }


@router.get("/health")
def health() -> dict:
    return {"ok": True}


@router.get("/status")
def status(engine: BotEngine = Depends(get_engine)) -> dict:
    return engine.status()


@router.get("/trades")
def trades(limit: int = 50, engine: BotEngine = Depends(get_engine)) -> list[dict]:
    return [_trade_dict(t) for t in engine.db.recent_trades(min(limit, 500), mode=engine.book)]


@router.get("/signals")
def signals(limit: int = 15, engine: BotEngine = Depends(get_engine)) -> list[dict]:
    scan = engine.last_scan
    if scan is not None:  # bu oturumdaki son tarama (eski kayıtlar karışmasın)
        regime = engine.regime.regime.value if engine.regime else ""
        return [
            {"symbol": s.symbol, "score": s.score, "price": s.price, "eligible": s.eligible,
             "regime": regime, "components": s.components, "reasons": s.reasons, "created_at": None}
            for s in scan.scored[: min(limit, 50)]
        ]
    return [
        {"symbol": s.symbol, "score": s.score, "price": s.price, "eligible": s.eligible,
         "regime": s.regime, "components": s.components, "reasons": s.reasons,
         "created_at": s.created_at.isoformat()}
        for s in engine.db.latest_signals(min(limit, 50))
    ]


@router.get("/logs")
def logs(limit: int = 50, engine: BotEngine = Depends(get_engine)) -> list[dict]:
    return [
        {"time": l.created_at.isoformat(), "level": l.level, "category": l.category,
         "message": l.message}
        for l in engine.db.recent_logs(min(limit, 500))
    ]


@router.get("/daily-stats")
def daily_stats(engine: BotEngine = Depends(get_engine)) -> list[dict]:
    return [
        {"day": d.day, "start_equity": d.start_equity, "end_equity": d.end_equity,
         "realized_pnl": d.realized_pnl, "fees_usdt": d.fees_usdt, "trades_opened": d.trades_opened,
         "trades_closed": d.trades_closed, "wins": d.wins, "losses": d.losses}
        for d in engine.db.daily_stats()
    ]


@router.post("/start", dependencies=[Depends(require_token)])
def start(engine: BotEngine = Depends(get_engine)) -> dict:
    try:
        engine.start()
    except SafetyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "running": engine.running}


@router.post("/stop", dependencies=[Depends(require_token)])
def stop(engine: BotEngine = Depends(get_engine)) -> dict:
    engine.stop()
    return {"ok": True, "running": engine.running}


@router.post("/emergency-stop", dependencies=[Depends(require_token)])
def emergency_stop(engine: BotEngine = Depends(get_engine)) -> dict:
    engine.emergency_stop()
    return {"ok": True, "emergency_stop": True}


@router.post("/emergency-reset", dependencies=[Depends(require_token)])
def emergency_reset(body: ConfirmBody, engine: BotEngine = Depends(get_engine)) -> dict:
    if body.confirm.strip().upper() != RESET_EMERGENCY_CONFIRM_TEXT:
        raise HTTPException(status_code=400, detail=f"Onay için '{RESET_EMERGENCY_CONFIRM_TEXT}' yazın")
    engine.reset_emergency()
    return {"ok": True, "emergency_stop": False}


@router.post("/close-all", dependencies=[Depends(require_token)])
def close_all(body: ConfirmBody, engine: BotEngine = Depends(get_engine)) -> dict:
    if body.confirm.strip().upper() != CLOSE_ALL_CONFIRM_TEXT:
        raise HTTPException(status_code=400, detail=f"Onay için '{CLOSE_ALL_CONFIRM_TEXT}' yazın")
    results = engine.close_all_positions()
    return {"ok": True, "results": results}
