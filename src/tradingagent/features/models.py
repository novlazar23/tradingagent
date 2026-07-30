"""Framework-independent feature contracts and explicit configuration."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class IndicatorConfig:
    """All periods and thresholds required by the deterministic indicator set."""

    sma_period: int
    ema_period: int
    rsi_period: int
    macd_fast_period: int
    macd_slow_period: int
    macd_signal_period: int
    bollinger_period: int
    bollinger_stddev: Decimal
    atr_period: int
    volume_period: int
    rsi_oversold: Decimal
    rsi_overbought: Decimal
    version: str = "indicators-v1"

    def __post_init__(self) -> None:
        periods = (
            self.sma_period,
            self.ema_period,
            self.rsi_period,
            self.macd_fast_period,
            self.macd_slow_period,
            self.macd_signal_period,
            self.bollinger_period,
            self.atr_period,
            self.volume_period,
        )
        if any(period <= 0 for period in periods):
            raise ValueError("indicator periods must be positive")
        if self.macd_fast_period >= self.macd_slow_period:
            raise ValueError("MACD fast period must be shorter than slow period")
        if not Decimal(0) <= self.rsi_oversold < self.rsi_overbought <= Decimal(100):
            raise ValueError("RSI thresholds must be ordered within [0, 100]")
        if self.bollinger_stddev <= 0:
            raise ValueError("Bollinger standard-deviation multiplier must be positive")

    @property
    def warmup(self) -> int:
        """Return the minimum candle count before every result is tradeable."""
        return max(
            self.sma_period,
            self.ema_period,
            self.rsi_period + 1,
            self.macd_slow_period + self.macd_signal_period,
            self.bollinger_period,
            self.atr_period + 1,
            self.volume_period,
        )


@dataclass(frozen=True, slots=True)
class IndicatorContribution:
    """One normalized, auditable indicator input to strategy scoring."""

    score: Decimal
    reason: str
    raw_values: dict[str, Decimal | None]

    def __post_init__(self) -> None:
        if not Decimal("-1") <= self.score <= Decimal("1"):
            raise ValueError("indicator contribution must be normalized to [-1, 1]")
        if not self.reason or not self.raw_values:
            raise ValueError("indicator contribution requires reason and raw values")


@dataclass(frozen=True, slots=True)
class IndicatorResult:
    """Latest raw indicator snapshot plus normalized strategy evidence."""

    timeframe: str
    available_at: datetime
    version: str
    tradeable: bool
    contribution: Decimal
    reason: str
    sma: Decimal | None = None
    ema: Decimal | None = None
    rsi: Decimal | None = None
    macd_line: Decimal | None = None
    macd_signal: Decimal | None = None
    macd_histogram: Decimal | None = None
    bollinger_middle: Decimal | None = None
    bollinger_upper: Decimal | None = None
    bollinger_lower: Decimal | None = None
    bollinger_bandwidth: Decimal | None = None
    bollinger_position: Decimal | None = None
    atr: Decimal | None = None
    volume_average: Decimal | None = None
    relative_volume: Decimal | None = None
    raw_values: dict[str, Decimal | None] = field(default_factory=dict)
    contributions: dict[str, IndicatorContribution] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PatternConfig:
    """Explicit tolerances controlling pivots and pattern confirmation."""

    pivot_window: int = 2
    similar_level_tolerance: Decimal = Decimal("0.03")
    minimum_separation: int = 2
    breakout_tolerance: Decimal = Decimal("0")
    doji_body_ratio: Decimal = Decimal("0.1")
    wick_body_ratio: Decimal = Decimal("2")
    volume_confirmation_ratio: Decimal = Decimal("1")
    version: str = "patterns-v1"

    def __post_init__(self) -> None:
        if self.pivot_window < 1 or self.minimum_separation < 1:
            raise ValueError("pivot window and separation must be positive")
        if (
            min(
                self.similar_level_tolerance,
                self.breakout_tolerance,
                self.doji_body_ratio,
                self.wick_body_ratio,
                self.volume_confirmation_ratio,
            )
            < 0
        ):
            raise ValueError("pattern tolerances cannot be negative")


PatternDirection = Literal["bullish", "bearish", "neutral"]


@dataclass(frozen=True, slots=True)
class PatternDetection:
    """Auditable pattern known only from ``available_at`` onward."""

    name: str
    direction: PatternDirection
    confidence: Decimal
    start_time: datetime
    end_time: datetime
    available_at: datetime
    timeframe: str
    confirmed: bool
    price_levels: dict[str, Decimal]
    invalidation_level: Decimal | None
    evidence: tuple[str, ...]
    version: str
