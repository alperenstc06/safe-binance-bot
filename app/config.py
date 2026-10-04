"""Uygulama ayarları.

Tüm ayarlar ortam değişkenlerinden / .env dosyasından okunur.
API anahtarları ASLA kaynak koda yazılmaz.
Varsayılan çalışma modu DRY_RUN'dır; LIVE moda yalnızca TRADING_MODE=LIVE ile geçilir.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"


class Exchange(str, Enum):
    BINANCE_GLOBAL = "BINANCE_GLOBAL"
    BINANCE_TR = "BINANCE_TR"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Binance bağlantısı ---
    binance_api_key: SecretStr = SecretStr("")
    binance_api_secret: SecretStr = SecretStr("")
    binance_base_url: str = "https://api.binance.com"
    binance_testnet: bool = False
    binance_testnet_url: str = "https://testnet.binance.vision"
    binance_timeout_seconds: int = 15
    binance_recv_window: int = 5000

    # --- Binance TR bağlantısı ---
    binance_tr_api_key: SecretStr = SecretStr("")
    binance_tr_api_secret: SecretStr = SecretStr("")
    binance_tr_base_url: str = "https://www.binance.tr"
    binance_tr_market_data_url: str = "https://api.binance.com"

    # --- İşlem yapılacak borsa (panelde iki hesap da gösterilir) ---
    trading_exchange: Exchange = Exchange.BINANCE_GLOBAL

    # --- Çalışma modu ---
    trading_mode: TradingMode = TradingMode.DRY_RUN
    auto_start: bool = True
    allow_live_auto_start: bool = False
    dry_run_start_balance: float = 1000.0

    # --- Döngü ---
    loop_interval_seconds: int = 30
    scan_interval_seconds: int = 300

    # --- Risk profili ---
    max_position_pct: float = 0.20
    max_open_positions: int = 1
    risk_per_trade_pct: float = 0.01
    max_daily_loss_pct: float = 0.03
    max_daily_trades: int = 5
    max_consecutive_losses: int = 3
    cooldown_hours: float = 12.0
    min_opportunity_score: float = 75.0

    # --- Strateji / çıkış ---
    kline_interval: str = "1h"
    kline_limit: int = 300
    atr_period: int = 14
    stop_atr_multiplier: float = 2.0
    breakeven_trigger_atr: float = 1.0
    trailing_trigger_atr: float = 1.5
    trailing_atr_multiplier: float = 2.0
    # Kâr büyüdükçe trailing daralır: kâr >= TRAIL_TIGHTEN_AFTER_R x risk olunca bu çarpan kullanılır
    trail_tighten_after_r: float = 2.0
    trailing_atr_multiplier_tight: float = 1.2
    # İğne (wick) önlemleri
    wick_lookback: int = 48
    wick_atr_threshold: float = 1.5
    max_wick_count: int = 3
    max_single_wick_atr: float = 3.0
    stop_swing_buffer_atr: float = 0.2
    max_stop_atr: float = 3.0
    max_entry_drift_pct: float = 0.01
    # Kısmi kâr alma: fiyat giriş + PARTIAL_TP_R_MULTIPLE x risk olunca pozisyonun bir kısmı satılır
    partial_take_profit_enabled: bool = True
    partial_tp_r_multiple: float = 1.5
    partial_tp_fraction: float = 0.5
    reward_risk_ratio: float = 2.0
    max_extension_atr: float = 2.5

    # --- Maliyetler ---
    fee_rate: float = 0.001
    slippage_pct: float = 0.0005
    min_expected_edge_pct: float = 0.003
    min_cost_coverage: float = 3.0

    # --- Tarayıcı filtreleri ---
    quote_asset: str = "USDT"
    min_quote_volume_usdt: float = 20_000_000.0
    max_spread_pct: float = 0.0015
    max_abs_24h_change_pct: float = 15.0
    max_last_candle_change_pct: float = 5.0
    min_listing_days: int = 30
    max_candidates: int = 25
    symbol_blacklist: str = ""

    # --- Piyasa rejimi ---
    regime_interval: str = "4h"
    high_volatility_atr_pct: float = 0.04
    high_volatility_24h_change_pct: float = 8.0
    neutral_size_multiplier: float = 0.5
    neutral_extra_score: float = 5.0

    # --- Portföy rotasyonu ---
    rotation_enabled: bool = True
    rotation_min_score_diff: float = 15.0
    rotation_min_hold_minutes: int = 60
    manage_existing_holdings: bool = False
    holding_confirm_cycles: int = 3
    partial_sell_fraction: float = 0.5
    min_holding_value_usdt: float = 10.0

    # --- Emir güvenliği ---
    place_exchange_stop: bool = True
    stop_limit_offset_pct: float = 0.005

    # --- Telefondan takip (Telegram) ---
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    telegram_commands_enabled: bool = True

    # --- Panel / veritabanı ---
    database_url: str = "sqlite:///./data/bot.db"
    panel_host: str = "127.0.0.1"
    panel_port: int = 8000
    panel_token: SecretStr = SecretStr("")
    panel_insecure_ok: bool = False  # yalnızca Docker'da port 127.0.0.1'e bağlıyken
    log_dir: str = "logs"
    log_level: str = "INFO"

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _normalize_mode(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().upper()
            if value not in ("DRY_RUN", "LIVE"):
                raise ValueError("TRADING_MODE yalnızca DRY_RUN veya LIVE olabilir")
        return value

    @field_validator("trading_exchange", mode="before")
    @classmethod
    def _normalize_exchange(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().upper().replace("-", "_")
            aliases = {"GLOBAL": "BINANCE_GLOBAL", "BINANCE": "BINANCE_GLOBAL", "TR": "BINANCE_TR"}
            value = aliases.get(value, value)
            if value not in ("BINANCE_GLOBAL", "BINANCE_TR"):
                raise ValueError("TRADING_EXCHANGE yalnızca BINANCE_GLOBAL veya BINANCE_TR olabilir")
        return value

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("MAX_POSITION_PCT 0 ile 1 arasında olmalı")
        if not 0 < self.risk_per_trade_pct <= 0.05:
            raise ValueError("RISK_PER_TRADE_PCT 0 ile 0.05 arasında olmalı")
        if not 0 < self.max_daily_loss_pct <= 0.2:
            raise ValueError("MAX_DAILY_LOSS_PCT 0 ile 0.2 arasında olmalı")
        if self.max_open_positions < 1:
            raise ValueError("MAX_OPEN_POSITIONS en az 1 olmalı")
        if not 0 < self.partial_tp_fraction < 1:
            raise ValueError("PARTIAL_TP_FRACTION 0 ile 1 arasında olmalı")
        if self.trailing_atr_multiplier_tight <= 0 or self.trailing_atr_multiplier_tight > self.trailing_atr_multiplier:
            raise ValueError("TRAILING_ATR_MULTIPLIER_TIGHT pozitif ve normal çarpandan büyük olmamalı")
        if self.stop_atr_multiplier <= 0 or self.trailing_atr_multiplier <= 0:
            raise ValueError("ATR çarpanları pozitif olmalı")
        # Binance TR'de pariteler TL (TRY) bazlıdır; açıkça ayarlanmadıysa TRY kullanılır
        if self.is_tr and "quote_asset" not in self.model_fields_set:
            self.quote_asset = "TRY"
        self.quote_asset = self.quote_asset.strip().upper()
        # TL piyasasının hacmi USDT piyasasından çok düşüktür: TR için varsayılan eşik 1M USDT
        if self.is_tr and "min_quote_volume_usdt" not in self.model_fields_set:
            self.min_quote_volume_usdt = 1_000_000.0
        if (self.panel_host not in ("127.0.0.1", "localhost", "::1")
                and not self.panel_token.get_secret_value().strip() and not self.panel_insecure_ok):
            raise ValueError("Panel ağa açılıyorsa (PANEL_HOST=0.0.0.0) PANEL_TOKEN tanımlanmalı")
        if self.trading_mode == TradingMode.LIVE and not self.has_trading_keys:
            names = ("BINANCE_TR_API_KEY ve BINANCE_TR_API_SECRET" if self.is_tr
                     else "BINANCE_API_KEY ve BINANCE_API_SECRET")
            raise ValueError(f"LIVE mod için {names} .env içinde tanımlı olmalı")
        return self

    @property
    def has_api_keys(self) -> bool:
        return bool(
            self.binance_api_key.get_secret_value().strip()
            and self.binance_api_secret.get_secret_value().strip()
        )

    @property
    def has_tr_api_keys(self) -> bool:
        return bool(
            self.binance_tr_api_key.get_secret_value().strip()
            and self.binance_tr_api_secret.get_secret_value().strip()
        )

    @property
    def is_tr(self) -> bool:
        return self.trading_exchange == Exchange.BINANCE_TR

    @property
    def has_trading_keys(self) -> bool:
        return self.has_tr_api_keys if self.is_tr else self.has_api_keys

    @property
    def is_live(self) -> bool:
        return self.trading_mode == TradingMode.LIVE

    @property
    def effective_base_url(self) -> str:
        return self.binance_testnet_url if self.binance_testnet else self.binance_base_url

    @property
    def blacklist(self) -> set[str]:
        return {s.strip().upper() for s in self.symbol_blacklist.split(",") if s.strip()}

    @property
    def round_trip_cost_pct(self) -> float:
        """Alış + satış komisyonu ve her iki taraf slippage toplamı (oran)."""
        return 2 * self.fee_rate + 2 * self.slippage_pct

    def public_summary(self) -> dict:
        """Panelde gösterilebilecek, gizli bilgi içermeyen ayar özeti."""
        return {
            "trading_mode": self.trading_mode.value,
            "trading_exchange": self.trading_exchange.value,
            "quote_asset": self.quote_asset,
            "has_tr_api_keys": self.has_tr_api_keys,
            "testnet": self.binance_testnet,
            "has_api_keys": self.has_api_keys,
            "max_position_pct": self.max_position_pct,
            "max_open_positions": self.max_open_positions,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_daily_trades": self.max_daily_trades,
            "max_consecutive_losses": self.max_consecutive_losses,
            "cooldown_hours": self.cooldown_hours,
            "min_opportunity_score": self.min_opportunity_score,
            "fee_rate": self.fee_rate,
            "slippage_pct": self.slippage_pct,
            "rotation_enabled": self.rotation_enabled,
            "manage_existing_holdings": self.manage_existing_holdings,
            "place_exchange_stop": self.place_exchange_stop,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()

