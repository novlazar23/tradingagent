from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select

from tradingagent.api.services import ApplicationRegistry
from tradingagent.config import AppConfig
from tradingagent.persistence.bootstrap import bootstrap_environment
from tradingagent.persistence.models import Base, MarketDataset


class History:
    def list_datasets(self) -> tuple[str, ...]:
        return ("other.data", "btc-selected.data")


def config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "deployment": {
                "history_base_url": "http://history-proxy:8080",
                "history_api_key_file": "/run/secrets/octobot_history_api_key",
                "history_page_limit": 500,
                "backtest_max_candles": 100000,
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
                "version": "bootstrap-v1",
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
    )


def test_bootstrap_registers_explicit_octobot_dataset_and_immutable_configuration() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    registry = ApplicationRegistry(engine=engine)

    result = bootstrap_environment(
        registry,
        History(),
        config(),
        external_dataset_id="btc-selected.data",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 7, 1, tzinfo=UTC),
        code_version="test-code",
    )

    assert result["external_dataset_id"] == "btc-selected.data"
    assert len(result["configuration_version"]) == 64
    with registry.sessions() as db:
        dataset = db.scalar(select(MarketDataset))
        assert dataset is not None
        assert dataset.id == result["dataset_id"]
        assert dataset.metadata_json["start"] == "2026-01-01T00:00:00+00:00"


def test_bootstrap_rejects_unknown_dataset_instead_of_selecting_implicitly() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with pytest.raises(ValueError, match="not available"):
        bootstrap_environment(
            ApplicationRegistry(engine=engine),
            History(),
            config(),
            external_dataset_id="missing.data",
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 7, 1, tzinfo=UTC),
            code_version="test-code",
        )
