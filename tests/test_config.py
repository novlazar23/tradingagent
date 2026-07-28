from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from tradingagent.config import AppConfig, database_url_from_environment


def valid_config() -> dict[str, object]:
    return {
        "deployment": {
            "history_base_url": "http://192.168.178.20:5002",
            "history_api_key_file": "/run/secrets/octobot_history_api_key",
            "history_page_limit": 500,
            "paper_poll_seconds": 30,
            "paper_max_candle_age_seconds": 1200,
        },
        "costs": {
            "maker_fee_rate": "0.001",
            "taker_fee_rate": "0.001",
            "spread_bps": "2",
            "slippage_model": "fixed_bps",
            "slippage_bps": "3",
            "atr_slippage_multiplier": None,
            "price_quantum": "0.01",
            "quantity_quantum": "0.000001",
            "minimum_order_value": "10",
            "rounding_mode": "ROUND_DOWN",
        },
        "risk": {
            "initial_capital": "10000",
            "maximum_position_fraction": "0.5",
            "risk_per_trade_fraction": "0.01",
            "stop_mode": "percent",
            "stop_distance": "0.02",
            "take_profit_distance": "0.04",
            "maximum_daily_loss_fraction": "0.03",
            "maximum_session_drawdown_fraction": "0.1",
            "maximum_entries_per_utc_day": 3,
            "cooldown_seconds": 900,
            "minimum_cash_reserve_fraction": "0.1",
        },
        "strategy": {
            "version": "test-v1",
            "timeframe_weights": {"15m": "0.4", "1h": "0.3", "4h": "0.2", "1d": "0.1"},
            "indicator_parameters": {"rsi_period": "14"},
            "pattern_parameters": {"pivot_window": "3"},
            "entry_threshold": "0.6",
            "exit_threshold": "-0.4",
            "minimum_confidence": "0.5",
            "minimum_confirming_groups": 2,
            "cooldown_seconds": 900,
            "higher_timeframe_mode": "weighted",
        },
    }


def test_history_page_limit_is_capped_at_upstream_contract() -> None:
    payload = valid_config()
    payload["deployment"]["history_page_limit"] = 501  # type: ignore[index]

    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_configuration_requires_complete_explicit_values() -> None:
    payload = valid_config()
    del payload["risk"]

    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


def test_configuration_rejects_unknown_fields() -> None:
    payload = valid_config()
    payload["surprise"] = True

    with pytest.raises(ValidationError, match="surprise"):
        AppConfig.model_validate(payload)


def test_configuration_rejects_negative_costs_and_invalid_limits() -> None:
    payload = valid_config()
    payload["costs"]["spread_bps"] = "-1"  # type: ignore[index]
    payload["risk"]["maximum_position_fraction"] = "1.1"  # type: ignore[index]

    with pytest.raises(ValidationError) as error:
        AppConfig.model_validate(payload)

    assert {item["loc"] for item in error.value.errors()} >= {
        ("costs", "spread_bps"),
        ("risk", "maximum_position_fraction"),
    }


def test_configuration_preserves_decimal_values() -> None:
    config = AppConfig.model_validate(valid_config())

    assert config.risk.initial_capital == Decimal("10000")
    assert isinstance(config.costs.taker_fee_rate, Decimal)


def test_database_url_never_accepts_plaintext_environment_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://agent:plaintext@db/agent")

    assert database_url_from_environment() is None


def test_database_url_is_assembled_only_from_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / "postgres-password"
    secret.write_text("s/ecret\n", encoding="utf-8")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("DATABASE_USER", "agent")
    monkeypatch.setenv("DATABASE_HOST", "database")
    monkeypatch.setenv("DATABASE_PORT", "5433")
    monkeypatch.setenv("DATABASE_NAME", "trading")

    assert database_url_from_environment() == (
        "postgresql+psycopg://agent:s%2Fecret@database:5433/trading"
    )
