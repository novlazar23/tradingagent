"""Strict, immutable configuration contracts for reproducible trading runs."""

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator


class StrictConfigModel(BaseModel):
    """Base for immutable configuration objects that reject unknown fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DeploymentConfig(StrictConfigModel):
    """Runtime connectivity and paper-data freshness settings."""

    history_base_url: AnyHttpUrl
    history_api_key_file: str = Field(min_length=1)
    history_page_limit: int = Field(gt=0, le=500)
    paper_poll_seconds: int = Field(gt=0)
    paper_max_candle_age_seconds: int = Field(gt=0)


class CostConfig(StrictConfigModel):
    """Explicit execution costs and exchange precision constraints."""

    maker_fee_rate: Decimal = Field(ge=0)
    taker_fee_rate: Decimal = Field(ge=0)
    spread_bps: Decimal = Field(ge=0)
    slippage_model: Literal["fixed_bps", "atr_scaled"]
    slippage_bps: Decimal = Field(ge=0)
    atr_slippage_multiplier: Decimal | None
    price_quantum: Decimal = Field(gt=0)
    quantity_quantum: Decimal = Field(gt=0)
    minimum_order_value: Decimal = Field(gt=0)
    rounding_mode: Literal["ROUND_DOWN", "ROUND_HALF_EVEN"]

    @model_validator(mode="after")
    def require_atr_multiplier(self) -> "CostConfig":
        """Require a positive multiplier exactly when ATR slippage is selected."""
        if self.slippage_model == "atr_scaled":
            if self.atr_slippage_multiplier is None or self.atr_slippage_multiplier <= 0:
                raise ValueError("atr_slippage_multiplier must be positive for atr_scaled")
        elif self.atr_slippage_multiplier is not None:
            raise ValueError("atr_slippage_multiplier is only valid for atr_scaled")
        return self


class RiskConfig(StrictConfigModel):
    """Mandatory long/flat portfolio and loss controls."""

    initial_capital: Decimal = Field(gt=0)
    maximum_position_fraction: Decimal = Field(gt=0, le=1)
    risk_per_trade_fraction: Decimal = Field(gt=0, le=1)
    stop_mode: Literal["atr", "percent"]
    stop_distance: Decimal = Field(gt=0)
    take_profit_distance: Decimal | None = Field(gt=0)
    maximum_daily_loss_fraction: Decimal = Field(gt=0, le=1)
    maximum_session_drawdown_fraction: Decimal = Field(gt=0, le=1)
    maximum_entries_per_utc_day: int = Field(gt=0)
    cooldown_seconds: int = Field(ge=0)
    minimum_cash_reserve_fraction: Decimal = Field(ge=0, lt=1)


class StrategyConfig(StrictConfigModel):
    """Versioned strategy inputs; no trading parameter has a hidden default."""

    version: str = Field(min_length=1)
    timeframe_weights: dict[Literal["15m", "1h", "4h", "1d"], Decimal]
    indicator_parameters: dict[str, Decimal]
    pattern_parameters: dict[str, Decimal]
    entry_threshold: Decimal = Field(ge=-1, le=1)
    exit_threshold: Decimal = Field(ge=-1, le=1)
    minimum_confidence: Decimal = Field(ge=0, le=1)
    minimum_confirming_groups: int = Field(gt=0)
    cooldown_seconds: int = Field(ge=0)
    higher_timeframe_mode: Literal["weighted", "hard_filter"]

    @model_validator(mode="after")
    def validate_timeframes(self) -> "StrategyConfig":
        """Ensure all four native timeframes are explicitly weighted."""
        required = {"15m", "1h", "4h", "1d"}
        if set(self.timeframe_weights) != required:
            raise ValueError(f"timeframe_weights must contain exactly {sorted(required)}")
        if any(weight < 0 for weight in self.timeframe_weights.values()):
            raise ValueError("timeframe weights cannot be negative")
        if sum(self.timeframe_weights.values()) <= 0:
            raise ValueError("at least one timeframe weight must be positive")
        return self


class AppConfig(StrictConfigModel):
    """Complete configuration required to start a tradingagent process."""

    deployment: DeploymentConfig
    costs: CostConfig
    risk: RiskConfig
    strategy: StrategyConfig


class StrategyValidationConfig(StrictConfigModel):
    """Safe, deployment-secret-free strategy validation envelope."""

    strategy: StrategyConfig
    risk: RiskConfig
    costs: CostConfig


def database_url_from_environment() -> str | None:
    """Assemble the database URL only in process memory from a Docker secret."""
    password_file = os.getenv("DATABASE_PASSWORD_FILE")
    if not password_file:
        return None
    password = Path(password_file).read_text(encoding="utf-8").strip()
    if not password:
        raise RuntimeError("Database password secret is empty")
    user = quote(os.getenv("DATABASE_USER", "tradingagent"), safe="")
    password = quote(password, safe="")
    host = os.getenv("DATABASE_HOST", "postgres")
    port = os.getenv("DATABASE_PORT", "5432")
    name = quote(os.getenv("DATABASE_NAME", "tradingagent"), safe="")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{name}"
