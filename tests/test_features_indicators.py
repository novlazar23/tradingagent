from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradingagent.domain import Candle
from tradingagent.features import (
    IndicatorConfig,
    align_closed_candles,
    calculate_indicators,
)


def candle(index: int, close: str, *, timeframe: str = "15m", volume: str = "10") -> Candle:
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * index)
    price = Decimal(close)
    return Candle(
        source="fixture",
        dataset_id="golden",
        symbol="BTC/USDT",
        timeframe=timeframe,  # type: ignore[arg-type]
        open_time=opened,
        close_time=opened + timedelta(minutes=15),
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=Decimal(volume),
        is_closed=True,
        source_fingerprint=str(index),
        ingested_at=opened,
    )


def test_indicators_match_small_independent_golden_values() -> None:
    candles = [candle(i, str(i + 10), volume=str(i + 1)) for i in range(6)]
    result = calculate_indicators(
        candles,
        IndicatorConfig(
            sma_period=3,
            ema_period=3,
            rsi_period=3,
            macd_fast_period=2,
            macd_slow_period=3,
            macd_signal_period=2,
            bollinger_period=3,
            bollinger_stddev=Decimal("2"),
            atr_period=3,
            volume_period=3,
            rsi_oversold=Decimal("30"),
            rsi_overbought=Decimal("70"),
        ),
    )

    assert result.sma == Decimal("14")
    assert result.ema == Decimal("14.03125")
    assert result.rsi == Decimal("100")
    assert result.atr == Decimal("2")
    assert result.relative_volume == Decimal("1.2")
    assert result.bollinger_middle == Decimal("14")
    assert abs(result.bollinger_upper - Decimal("15.632993161855452")) < Decimal("1e-14")
    assert result.available_at == candles[-1].close_time
    assert result.raw_values["macd_histogram"] == result.macd_histogram
    assert Decimal("-1") <= result.contribution <= Decimal("1")


def test_indicators_are_not_tradeable_before_full_warmup() -> None:
    result = calculate_indicators(
        [candle(i, str(i + 10)) for i in range(3)],
        IndicatorConfig(
            sma_period=3,
            ema_period=3,
            rsi_period=3,
            macd_fast_period=2,
            macd_slow_period=3,
            macd_signal_period=2,
            bollinger_period=3,
            bollinger_stddev=Decimal("2"),
            atr_period=3,
            volume_period=3,
            rsi_oversold=Decimal("30"),
            rsi_overbought=Decimal("70"),
        ),
    )
    assert result.tradeable is False
    assert "warm-up" in result.reason


def test_alignment_never_exposes_open_or_future_candles() -> None:
    candles = [candle(i, str(100 + i)) for i in range(4)]
    future = candles[-1]
    decision_time = candles[-2].close_time

    assert align_closed_candles(candles, decision_time) == tuple(candles[:-1])
    assert future.close_time > decision_time
