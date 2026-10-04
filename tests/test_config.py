import pytest
from pydantic import ValidationError

from tests.conftest import make_settings


def test_default_mode_is_dry_run(monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.trading_mode.value == "DRY_RUN" and not s.is_live


def test_live_requires_keys():
    with pytest.raises(ValidationError):
        make_settings(trading_mode="LIVE")
    s = make_settings(trading_mode="live", binance_api_key="k", binance_api_secret="s")
    assert s.is_live


def test_invalid_mode_rejected():
    with pytest.raises(ValidationError):
        make_settings(trading_mode="FUTURES")


def test_secrets_not_in_public_summary():
    s = make_settings(binance_api_key="AAA", binance_api_secret="BBB")
    text = str(s.public_summary())
    assert "AAA" not in text and "BBB" not in text
    assert "AAA" not in repr(s)


def test_default_risk_profile():
    s = make_settings()
    assert s.max_position_pct == 0.20
    assert s.max_open_positions == 1
    assert s.risk_per_trade_pct == 0.01
    assert s.max_daily_loss_pct == 0.03
    assert s.max_daily_trades == 5
    assert s.max_consecutive_losses == 3
    assert s.cooldown_hours == 12
    assert s.min_opportunity_score == 75
