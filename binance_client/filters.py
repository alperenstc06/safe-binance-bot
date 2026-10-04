"""Binance exchangeInfo sembol filtreleri.

LOT_SIZE, MARKET_LOT_SIZE, PRICE_FILTER, MIN_NOTIONAL ve NOTIONAL filtrelerini okur,
emir miktarı ve fiyatını Binance kurallarına uygun şekilde yuvarlar.
Yuvarlama her zaman AŞAĞI yönlüdür (asla bakiyeden fazla miktar veya
istenenden daha riskli fiyat üretmez).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


class FilterError(ValueError):
    """Emir Binance filtrelerini karşılamıyor."""


@dataclass
class SymbolFilters:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str = "TRADING"
    tick_size: Decimal = Decimal("0")
    min_price: Decimal = Decimal("0")
    max_price: Decimal = Decimal("0")
    step_size: Decimal = Decimal("0")
    min_qty: Decimal = Decimal("0")
    max_qty: Decimal = Decimal("0")
    market_step_size: Decimal = Decimal("0")
    market_min_qty: Decimal = Decimal("0")
    market_max_qty: Decimal = Decimal("0")
    min_notional: Decimal = Decimal("0")
    max_notional: Decimal = Decimal("0")
    apply_min_to_market: bool = True
    order_types: list[str] = field(default_factory=list)
    is_spot_trading_allowed: bool = True

    @classmethod
    def from_exchange_info(cls, info: dict) -> "SymbolFilters":
        f = cls(
            symbol=info["symbol"],
            base_asset=info.get("baseAsset", ""),
            quote_asset=info.get("quoteAsset", ""),
            status=info.get("status", "TRADING"),
            order_types=list(info.get("orderTypes", [])),
            is_spot_trading_allowed=bool(info.get("isSpotTradingAllowed", True)),
        )
        for flt in info.get("filters", []):
            ftype = flt.get("filterType")
            if ftype == "PRICE_FILTER":
                f.tick_size = _d(flt.get("tickSize", "0"))
                f.min_price = _d(flt.get("minPrice", "0"))
                f.max_price = _d(flt.get("maxPrice", "0"))
            elif ftype == "LOT_SIZE":
                f.step_size = _d(flt.get("stepSize", "0"))
                f.min_qty = _d(flt.get("minQty", "0"))
                f.max_qty = _d(flt.get("maxQty", "0"))
            elif ftype == "MARKET_LOT_SIZE":
                f.market_step_size = _d(flt.get("stepSize", "0"))
                f.market_min_qty = _d(flt.get("minQty", "0"))
                f.market_max_qty = _d(flt.get("maxQty", "0"))
            elif ftype == "MIN_NOTIONAL":
                f.min_notional = max(f.min_notional, _d(flt.get("minNotional", "0")))
                f.apply_min_to_market = bool(flt.get("applyToMarket", True))
            elif ftype == "NOTIONAL":
                f.min_notional = max(f.min_notional, _d(flt.get("minNotional", "0")))
                f.max_notional = _d(flt.get("maxNotional", "0"))
                f.apply_min_to_market = bool(flt.get("applyMinToMarket", True))
        return f

    # --- Fiyat ---
    def round_price(self, price: float | Decimal) -> Decimal:
        """Fiyatı tickSize katına aşağı yuvarlar."""
        p = _floor_to_step(_d(price), self.tick_size)
        return p.normalize() if p != 0 else Decimal("0")

    def round_price_up(self, price: float | Decimal) -> Decimal:
        p = _ceil_to_step(_d(price), self.tick_size)
        return p.normalize() if p != 0 else Decimal("0")

    def validate_price(self, price: Decimal) -> None:
        if price <= 0:
            raise FilterError(f"{self.symbol}: fiyat pozitif olmalı")
        if self.min_price > 0 and price < self.min_price:
            raise FilterError(f"{self.symbol}: fiyat minPrice altında ({price} < {self.min_price})")
        if self.max_price > 0 and price > self.max_price:
            raise FilterError(f"{self.symbol}: fiyat maxPrice üstünde ({price} > {self.max_price})")
        if self.tick_size > 0 and (price / self.tick_size) % 1 != 0:
            raise FilterError(f"{self.symbol}: fiyat tickSize katı değil ({price})")

    # --- Miktar ---
    def _qty_rules(self, market: bool) -> tuple[Decimal, Decimal, Decimal]:
        step, min_q, max_q = self.step_size, self.min_qty, self.max_qty
        if market:
            if self.market_step_size > 0:
                step = max(step, self.market_step_size)
            if self.market_min_qty > 0:
                min_q = max(min_q, self.market_min_qty)
            if self.market_max_qty > 0:
                max_q = min(max_q, self.market_max_qty) if max_q > 0 else self.market_max_qty
        return step, min_q, max_q

    def round_qty(self, qty: float | Decimal, market: bool = False) -> Decimal:
        """Miktarı stepSize katına aşağı yuvarlar ve maxQty ile sınırlar."""
        step, _, max_q = self._qty_rules(market)
        q = _floor_to_step(_d(qty), step)
        if max_q > 0 and q > max_q:
            q = _floor_to_step(max_q, step)
        return q.normalize() if q != 0 else Decimal("0")

    def validate_order(self, qty: Decimal, price: Decimal, market: bool = False) -> None:
        """Miktar ve notional (miktar*fiyat) filtrelerini doğrular, uymuyorsa FilterError."""
        step, min_q, max_q = self._qty_rules(market)
        if qty <= 0:
            raise FilterError(f"{self.symbol}: miktar pozitif olmalı")
        if min_q > 0 and qty < min_q:
            raise FilterError(f"{self.symbol}: miktar minQty altında ({qty} < {min_q})")
        if max_q > 0 and qty > max_q:
            raise FilterError(f"{self.symbol}: miktar maxQty üstünde ({qty} > {max_q})")
        if step > 0 and (qty / step) % 1 != 0:
            raise FilterError(f"{self.symbol}: miktar stepSize katı değil ({qty})")
        notional = qty * price
        if self.min_notional > 0 and (not market or self.apply_min_to_market):
            if notional < self.min_notional:
                raise FilterError(
                    f"{self.symbol}: emir tutarı MIN_NOTIONAL altında "
                    f"({notional:.8f} < {self.min_notional})"
                )
        if self.max_notional > 0 and notional > self.max_notional:
            raise FilterError(f"{self.symbol}: emir tutarı maxNotional üstünde")

    def prepare_order(
        self, qty: float | Decimal, price: float | Decimal, market: bool = True
    ) -> tuple[Decimal, Decimal]:
        """Miktar ve fiyatı yuvarlar, doğrular. (miktar, fiyat) döndürür."""
        q = self.round_qty(qty, market=market)
        p = self.round_price(price)
        self.validate_order(q, p if p > 0 else _d(price), market=market)
        return q, p

    def supports(self, order_type: str) -> bool:
        return not self.order_types or order_type in self.order_types


def parse_exchange_info(exchange_info: dict) -> dict[str, SymbolFilters]:
    return {
        s["symbol"]: SymbolFilters.from_exchange_info(s)
        for s in exchange_info.get("symbols", [])
    }


def fmt_decimal(value: Decimal) -> str:
    """Binance'e gönderilecek bilimsel gösterimsiz sayı metni."""
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
