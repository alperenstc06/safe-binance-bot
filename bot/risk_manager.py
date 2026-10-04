"""Risk yönetimi: pozisyon boyutu, günlük limitler, ardışık kayıp soğuması ve stop mantığı.

Kurallar:
- Tek pozisyon en fazla portföyün MAX_POSITION_PCT kadarı (varsayılan %20)
- İşlem başına risk en fazla RISK_PER_TRADE_PCT (varsayılan %1), komisyon+slippage dahil
- Günlük zarar MAX_DAILY_LOSS_PCT (varsayılan %3) aşılırsa yeni işlem yok
- Günde en fazla MAX_DAILY_TRADES yeni işlem
- MAX_CONSECUTIVE_LOSSES ardışık kayıpta COOLDOWN_HOURS soğuma
- Stop ASLA genişletilmez (sadece yukarı taşınır)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from database.database import Database


@dataclass
class RiskCheck:
    allowed: bool
    reason: str


@dataclass
class SizeResult:
    quantity: float
    notional: float
    risk_usdt: float
    reason: str

    @property
    def ok(self) -> bool:
        return self.quantity > 0


@dataclass
class StopUpdate:
    stop: float
    highest: float
    breakeven_active: bool
    trailing_active: bool
    changed: bool


def utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def initial_stop_price(entry: float, atr_value: float, atr_multiplier: float) -> float:
    """ATR tabanlı ilk stop. Fiyatın altında ve pozitif olmalıdır."""
    stop = entry - atr_multiplier * atr_value
    if stop <= 0 or stop >= entry:
        raise ValueError("Geçersiz stop hesaplandı")
    return stop


def wick_aware_stop(entry: float, atr_value: float, atr_multiplier: float, recent_low: float,
                    buffer_atr: float = 0.2, max_stop_atr: float = 3.0) -> float:
    """İğneye dayanıklı ilk stop.

    Normal ATR stopu son mumların dibinin (iğneler dahil) üstünde kalıyorsa, stop o dibin
    biraz altına alınır; böylece yakın zamanda atılmış bir iğne seviyesine stop konmaz.
    Stop hiçbir zaman girişin `max_stop_atr` ATR'den fazla altına inmez.
    """
    base = initial_stop_price(entry, atr_value, atr_multiplier)
    if recent_low <= 0 or atr_value <= 0:
        return base
    swing = recent_low - buffer_atr * atr_value
    floor = entry - max_stop_atr * atr_value
    stop = max(min(base, swing), floor)
    if stop <= 0 or stop >= entry:
        return base
    return stop


def compute_stop_update(
    entry: float,
    current_stop: float,
    highest: float,
    price: float,
    atr_value: float,
    breakeven_active: bool,
    trailing_active: bool,
    breakeven_trigger_atr: float,
    trailing_trigger_atr: float,
    trailing_atr_multiplier: float,
    round_trip_cost_pct: float,
) -> StopUpdate:
    """Break-even ve ATR trailing stop güncellemesi.

    - Fiyat entry + breakeven_trigger_atr*ATR üstüne çıkınca stop, maliyetleri karşılayan
      başa baş seviyesine çekilir.
    - Fiyat entry + trailing_trigger_atr*ATR üstüne çıkınca trailing aktif olur:
      stop = en yüksek fiyat - trailing_atr_multiplier*ATR
    - Yeni stop hiçbir zaman mevcut stoptan düşük olamaz.
    """
    new_highest = max(highest, price)
    new_stop = current_stop
    be_active, tr_active = breakeven_active, trailing_active

    if atr_value > 0:
        if new_highest >= entry + breakeven_trigger_atr * atr_value:
            breakeven_level = entry * (1 + round_trip_cost_pct)
            if breakeven_level < price:
                new_stop = max(new_stop, breakeven_level)
                be_active = True
        if new_highest >= entry + trailing_trigger_atr * atr_value:
            tr_active = True
        if tr_active:
            trail = new_highest - trailing_atr_multiplier * atr_value
            if trail < price:
                new_stop = max(new_stop, trail)

    new_stop = max(new_stop, current_stop)  # stop asla genişletilmez
    return StopUpdate(
        stop=new_stop,
        highest=new_highest,
        breakeven_active=be_active,
        trailing_active=tr_active,
        changed=new_stop > current_stop,
    )


def stop_hit(price: float, stop: float) -> bool:
    return price <= stop


class RiskManager:
    def __init__(self, settings, db: Database, mode: str):
        self.s = settings
        self.db = db
        self.mode = mode

    # --- Pozisyon boyutu ---
    def position_size(
        self,
        equity: float,
        free_usdt: float,
        entry: float,
        stop: float,
        size_multiplier: float = 1.0,
    ) -> SizeResult:
        """Risk tabanlı pozisyon boyutu.

        miktar = (özsermaye * risk%) / (entry - stop + entry*maliyet%)
        tutar en fazla özsermaye*MAX_POSITION_PCT ve serbest USDT ile sınırlanır.
        """
        if equity <= 0 or entry <= 0 or stop <= 0 or stop >= entry:
            return SizeResult(0, 0, 0, "Geçersiz özsermaye/giriş/stop")
        if size_multiplier <= 0:
            return SizeResult(0, 0, 0, "Piyasa rejimi yeni pozisyona izin vermiyor")
        cost_per_unit = entry * self.s.round_trip_cost_pct
        risk_per_unit = (entry - stop) + cost_per_unit
        risk_budget = equity * self.s.risk_per_trade_pct
        qty = risk_budget / risk_per_unit
        max_notional = equity * self.s.max_position_pct
        # alış komisyonu için pay bırak
        usable_free = free_usdt / (1 + self.s.fee_rate + self.s.slippage_pct)
        notional = min(qty * entry, max_notional, usable_free) * min(size_multiplier, 1.0)
        if notional <= 0:
            return SizeResult(0, 0, 0, "Yeterli serbest USDT yok")
        qty = notional / entry
        return SizeResult(qty, notional, qty * risk_per_unit, "OK")

    # --- Limit kontrolleri ---
    def daily_pnl(self, now: datetime, unrealized_pnl: float) -> float:
        return self.db.realized_pnl_since(utc_day_start(now), self.mode) + unrealized_pnl

    def daily_loss_exceeded(self, now: datetime, day_start_equity: float,
                            unrealized_pnl: float) -> bool:
        if day_start_equity <= 0:
            return False
        return self.daily_pnl(now, unrealized_pnl) <= -self.s.max_daily_loss_pct * day_start_equity

    def trades_today(self, now: datetime) -> int:
        return self.db.trades_opened_since(utc_day_start(now), self.mode)

    def consecutive_losses(self) -> tuple[int, datetime | None]:
        streak = 0
        last_loss_time: datetime | None = None
        for t in self.db.closed_trades_desc(limit=self.s.max_consecutive_losses + 40, mode=self.mode):
            if (t.exit_reason or "").startswith("PARTIAL"):
                continue  # kısmi kapanışlar ayrı işlem sayılmaz
            if (t.pnl_usdt or 0.0) < 0:
                if last_loss_time is None:
                    last_loss_time = t.exit_time
                streak += 1
            else:
                break
        return streak, last_loss_time

    def cooldown_until(self) -> datetime | None:
        streak, last_loss = self.consecutive_losses()
        if streak >= self.s.max_consecutive_losses and last_loss is not None:
            return last_loss + timedelta(hours=self.s.cooldown_hours)
        return None

    def can_open_trade(
        self,
        now: datetime,
        open_positions: int,
        day_start_equity: float,
        unrealized_pnl: float,
        emergency_stop: bool,
    ) -> RiskCheck:
        if emergency_stop:
            return RiskCheck(False, "Acil durdurma aktif")
        if open_positions >= self.s.max_open_positions:
            return RiskCheck(False, f"Maksimum açık pozisyon sayısına ulaşıldı ({open_positions})")
        if self.daily_loss_exceeded(now, day_start_equity, unrealized_pnl):
            return RiskCheck(False, f"Günlük zarar limiti (%{self.s.max_daily_loss_pct * 100:.1f}) aşıldı")
        trades = self.trades_today(now)
        if trades >= self.s.max_daily_trades:
            return RiskCheck(False, f"Günlük maksimum işlem sayısına ulaşıldı ({trades})")
        until = self.cooldown_until()
        if until is not None and now < until:
            return RiskCheck(False, f"{self.s.max_consecutive_losses} ardışık kayıp: "
                                    f"soğuma {until:%Y-%m-%d %H:%M} UTC'ye kadar")
        return RiskCheck(True, "OK")
