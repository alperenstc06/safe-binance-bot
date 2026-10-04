"""SQLAlchemy veritabanı modelleri."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Trade(Base):
    """Bot tarafından açılan bir pozisyon (alış) ve kapanışı (satış)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    base_asset: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(10), index=True, default="OPEN")  # OPEN / CLOSED

    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_quote: Mapped[float] = mapped_column(Float)  # harcanan USDT (USDT komisyon dahil)
    entry_fee_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    entry_external_fee_usdt: Mapped[float] = mapped_column(Float, default=0.0)  # BNB vb.
    entry_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    score_at_entry: Mapped[float] = mapped_column(Float, default=0.0)

    atr_at_entry: Mapped[float] = mapped_column(Float, default=0.0)
    current_atr: Mapped[float] = mapped_column(Float, default=0.0)
    initial_stop: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float)
    breakeven_active: Mapped[bool] = mapped_column(Boolean, default=False)
    trailing_active: Mapped[bool] = mapped_column(Boolean, default=False)
    highest_price: Mapped[float] = mapped_column(Float)
    stop_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_order_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_quote: Mapped[float | None] = mapped_column(Float, nullable=True)  # alınan net USDT
    exit_fee_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    exit_external_fee_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    pnl_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def total_fee_usdt(self) -> float:
        return (self.entry_fee_usdt or 0.0) + (self.exit_fee_usdt or 0.0)


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    scan_id: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    score: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    regime: Mapped[str] = mapped_column(String(20))
    eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    components: Mapped[dict] = mapped_column(JSON, default=dict)
    reasons: Mapped[list] = mapped_column(JSON, default=list)


class DecisionLog(Base):
    __tablename__ = "decision_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10), default="INFO")
    category: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class BotStateKV(Base):
    """Basit anahtar/değer bot durumu (çalışıyor, acil durdurma, kağıt bakiye...)."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DailyStat(Base):
    __tablename__ = "daily_stats"

    day: Mapped[str] = mapped_column(String(32), primary_key=True)  # YYYY-MM-DD[@defter] (UTC)
    start_equity: Mapped[float] = mapped_column(Float)
    end_equity: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    trades_opened: Mapped[int] = mapped_column(Integer, default=0)
    trades_closed: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PnlSnapshot(Base):
    __tablename__ = "pnl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    equity_usdt: Mapped[float] = mapped_column(Float)
    usdt_balance: Mapped[float] = mapped_column(Float)
    daily_pnl: Mapped[float] = mapped_column(Float)
    total_pnl: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
