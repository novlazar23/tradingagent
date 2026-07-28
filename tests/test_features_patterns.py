from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradingagent.domain import Candle
from tradingagent.features import PatternConfig, PatternEngine


def candle(
    index: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    volume: str = "10",
) -> Candle:
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * index)
    return Candle(
        source="fixture",
        dataset_id="patterns",
        symbol="BTC/USDT",
        timeframe="15m",
        open_time=opened,
        close_time=opened + timedelta(minutes=15),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        is_closed=True,
        source_fingerprint=str(index),
        ingested_at=opened,
    )


def test_candlestick_patterns_explain_evidence() -> None:
    candles = [
        candle(0, "10", "11", "8", "9"),
        candle(1, "8.5", "11.5", "8", "11"),  # bullish engulfing
        candle(2, "10", "12", "8", "10.01"),  # doji
    ]
    found = PatternEngine(PatternConfig()).detect(candles, candles[-1].close_time)

    by_name = {pattern.name: pattern for pattern in found}
    assert {"bullish_engulfing", "doji"} <= by_name.keys()
    assert by_name["doji"].evidence
    assert by_name["doji"].available_at == candles[-1].close_time
    assert Decimal("0") <= by_name["doji"].confidence <= Decimal("1")


def test_double_bottom_is_hidden_until_breakout_confirmation_closes() -> None:
    candles = [
        candle(0, "12", "13", "11", "12"),
        candle(1, "11", "11.2", "9", "10"),
        candle(2, "10", "12", "9.8", "11.5"),
        candle(3, "11", "11.2", "9.1", "10"),
        candle(4, "10.5", "12.5", "10.4", "12.2", "20"),
    ]
    engine = PatternEngine(
        PatternConfig(
            pivot_window=1,
            similar_level_tolerance=Decimal("0.03"),
            minimum_separation=2,
            breakout_tolerance=Decimal("0"),
        ),
    )

    before = engine.detect(candles, candles[-2].close_time)
    after = engine.detect(candles, candles[-1].close_time)

    assert "double_bottom" not in {pattern.name for pattern in before}
    pattern = next(pattern for pattern in after if pattern.name == "double_bottom")
    assert pattern.confirmed is True
    assert pattern.available_at == candles[-1].close_time
    assert pattern.start_time == candles[1].open_time
    assert pattern.invalidation_level == candles[1].low


def test_chart_pattern_requires_configured_volume_confirmation() -> None:
    candles = [
        candle(0, "12", "13", "11", "12"),
        candle(1, "11", "11.2", "9", "10"),
        candle(2, "10", "12", "9.8", "11.5"),
        candle(3, "11", "11.2", "9.1", "10"),
        candle(4, "10.5", "12.5", "10.4", "12.2", "10"),
    ]
    config = PatternConfig(
        pivot_window=1,
        minimum_separation=2,
        volume_confirmation_ratio=Decimal("2"),
    )

    result = PatternEngine(config).detect(candles, candles[-1].close_time)

    assert not any(item.name == "double_bottom" for item in result)
