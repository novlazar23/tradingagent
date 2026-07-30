from datetime import UTC, datetime, timedelta

import pytest

from tradingagent.market_data.timeframes import (
    aligned_open,
    available_candles,
    timeframe_delta,
)


@pytest.mark.parametrize(
    ("timeframe", "instant", "expected"),
    [
        ("15m", datetime(2026, 3, 29, 1, 17, tzinfo=UTC), datetime(2026, 3, 29, 1, 15, tzinfo=UTC)),
        ("1h", datetime(2026, 10, 25, 1, 17, tzinfo=UTC), datetime(2026, 10, 25, 1, tzinfo=UTC)),
        ("4h", datetime(2026, 12, 31, 23, 59, tzinfo=UTC), datetime(2026, 12, 31, 20, tzinfo=UTC)),
        ("1d", datetime(2027, 1, 1, 0, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)),
    ],
)
def test_utc_alignment_ignores_dst_and_handles_boundaries(timeframe, instant, expected) -> None:
    assert aligned_open(instant, timeframe) == expected


def test_available_candles_excludes_future_and_unclosed(candle_factory) -> None:
    decision_time = datetime(2026, 1, 2, tzinfo=UTC)
    closed = candle_factory("1h", decision_time - timedelta(hours=1), is_closed=True)
    source_open = candle_factory("1h", decision_time - timedelta(hours=1), is_closed=False)
    future = candle_factory("1h", decision_time, is_closed=True)

    assert available_candles([future, source_open, closed], decision_time) == (closed,)


def test_timeframe_delta_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        timeframe_delta("5m")  # type: ignore[arg-type]
