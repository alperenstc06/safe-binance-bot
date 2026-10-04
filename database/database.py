"""Veritabanı bağlantısı ve yardımcı sorgular (SQLite varsayılan)."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator

from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from database.models import (
    Base,
    BotStateKV,
    DailyStat,
    DecisionLog,
    PnlSnapshot,
    Signal,
    Trade,
    utcnow,
)

logger = logging.getLogger(__name__)

PARTIAL_TP_REASON = "PARTIAL_TAKE_PROFIT"


def _ensure_sqlite_dir(url: str) -> None:
    if url.startswith("sqlite:///") and ":memory:" not in url:
        path = url.replace("sqlite:///", "", 1)
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)


class Database:
    def __init__(self, url: str):
        _ensure_sqlite_dir(url)
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url:
                from sqlalchemy.pool import StaticPool

                kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **kwargs)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # --- Bot durumu ---
    def get_state(self, key: str, default: Any = None) -> Any:
        with self.session() as s:
            row = s.get(BotStateKV, key)
            return default if row is None else row.value

    def set_state(self, key: str, value: Any) -> None:
        with self.session() as s:
            row = s.get(BotStateKV, key)
            if row is None:
                s.add(BotStateKV(key=key, value=value))
            else:
                row.value = value
                row.updated_at = utcnow()

    # --- Karar günlüğü ---
    def log_decision(self, category: str, message: str, data: dict | None = None,
                     level: str = "INFO") -> None:
        log_fn = getattr(logger, level.lower(), logger.info)
        log_fn("[%s] %s", category, message)
        with self.session() as s:
            s.add(DecisionLog(category=category, message=message, data=data, level=level))

    def recent_logs(self, limit: int = 50) -> list[DecisionLog]:
        with self.session() as s:
            return list(s.scalars(select(DecisionLog).order_by(DecisionLog.id.desc()).limit(limit)))

    # --- İşlemler ---
    def add_trade(self, trade: Trade) -> Trade:
        with self.session() as s:
            s.add(trade)
            s.flush()
            s.refresh(trade)
            return trade

    def save_trade(self, trade: Trade) -> Trade:
        with self.session() as s:
            merged = s.merge(trade)
            s.flush()
            return merged

    def get_trade(self, trade_id: int) -> Trade | None:
        with self.session() as s:
            return s.get(Trade, trade_id)

    def open_trades(self, mode: str | None = None) -> list[Trade]:
        with self.session() as s:
            q = select(Trade).where(Trade.status == "OPEN")
            if mode:
                q = q.where(Trade.mode == mode)
            return list(s.scalars(q.order_by(Trade.id)))

    def recent_trades(self, limit: int = 50, mode: str | None = None) -> list[Trade]:
        with self.session() as s:
            q = select(Trade)
            if mode:
                q = q.where(Trade.mode == mode)
            return list(s.scalars(q.order_by(Trade.id.desc()).limit(limit)))

    def closed_trades_desc(self, limit: int = 20, mode: str | None = None) -> list[Trade]:
        with self.session() as s:
            q = select(Trade).where(Trade.status == "CLOSED")
            if mode:
                q = q.where(Trade.mode == mode)
            return list(s.scalars(q.order_by(Trade.exit_time.desc(), Trade.id.desc()).limit(limit)))

    def trades_opened_since(self, since: datetime, mode: str | None = None) -> int:
        """Yeni açılan işlem sayısı (kısmi kâr kayıtları ayrı işlem sayılmaz)."""
        with self.session() as s:
            q = select(func.count(Trade.id)).where(
                Trade.entry_time >= since,
                or_(Trade.exit_reason.is_(None), Trade.exit_reason != PARTIAL_TP_REASON),
            )
            if mode:
                q = q.where(Trade.mode == mode)
            return int(s.scalar(q) or 0)

    def realized_pnl_since(self, since: datetime, mode: str | None = None) -> float:
        with self.session() as s:
            q = select(func.coalesce(func.sum(Trade.pnl_usdt), 0.0)).where(
                Trade.status == "CLOSED", Trade.exit_time >= since
            )
            if mode:
                q = q.where(Trade.mode == mode)
            return float(s.scalar(q) or 0.0)

    def total_realized_pnl(self, mode: str | None = None) -> float:
        with self.session() as s:
            q = select(func.coalesce(func.sum(Trade.pnl_usdt), 0.0)).where(Trade.status == "CLOSED")
            if mode:
                q = q.where(Trade.mode == mode)
            return float(s.scalar(q) or 0.0)

    def total_fees(self, mode: str | None = None) -> float:
        with self.session() as s:
            q = select(func.coalesce(func.sum(Trade.entry_fee_usdt + Trade.exit_fee_usdt), 0.0))
            if mode:
                q = q.where(Trade.mode == mode)
            return float(s.scalar(q) or 0.0)

    # --- Sinyaller ---
    def save_signals(self, signals: list[Signal]) -> None:
        if not signals:
            return
        with self.session() as s:
            s.add_all(signals)

    def latest_signals(self, limit: int = 15) -> list[Signal]:
        with self.session() as s:
            last_scan = s.scalar(select(Signal.scan_id).order_by(Signal.id.desc()).limit(1))
            if not last_scan:
                return []
            q = (select(Signal).where(Signal.scan_id == last_scan)
                 .order_by(Signal.score.desc()).limit(limit))
            return list(s.scalars(q))

    def prune(self, days: int = 30) -> None:
        cutoff = utcnow() - timedelta(days=days)
        with self.session() as s:
            s.query(Signal).filter(Signal.created_at < cutoff).delete()
            s.query(DecisionLog).filter(DecisionLog.created_at < cutoff).delete()
            s.query(PnlSnapshot).filter(PnlSnapshot.created_at < cutoff).delete()

    # --- Günlük istatistik ---
    def get_daily_stat(self, day: str) -> DailyStat | None:
        with self.session() as s:
            return s.get(DailyStat, day)

    def upsert_daily_stat(self, day: str, **fields: Any) -> DailyStat:
        with self.session() as s:
            row = s.get(DailyStat, day)
            if row is None:
                row = DailyStat(day=day, start_equity=fields.get("start_equity", 0.0),
                                end_equity=fields.get("end_equity", fields.get("start_equity", 0.0)))
                s.add(row)
            for k, v in fields.items():
                setattr(row, k, v)
            s.flush()
            return row

    def daily_stats(self, limit: int = 30) -> list[DailyStat]:
        with self.session() as s:
            return list(s.scalars(select(DailyStat).order_by(DailyStat.day.desc()).limit(limit)))

    def add_pnl_snapshot(self, **fields: Any) -> None:
        with self.session() as s:
            s.add(PnlSnapshot(**fields))
