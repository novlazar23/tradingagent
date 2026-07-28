"""UTC-only timeframe arithmetic and no-lookahead selection."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Literal

from tradingagent.domain.models import Candle

Timeframe = Literal["15m", "1h", "4h", "1d"]

_DURATIONS: dict[Timeframe, timedelta] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}


def timeframe_delta(timeframe: Timeframe) -> timedelta:
    """Return the fixed UTC duration of a supported native timeframe."""
    try:
        return _DURATIONS[timeframe]
    except KeyError as error:
        raise ValueError(f"unsupported timeframe: {timeframe}") from error


def aligned_open(instant: datetime, timeframe: Timeframe) -> datetime:
    """Floor a UTC instant to its native candle boundary.

    Daily boundaries are midnight UTC. Because all arithmetic is UTC, local
    daylight-saving transitions cannot change the result.
    """
    if instant.tzinfo is None or instant.utcoffset() != timedelta(0):
        raise ValueError("instant must be timezone-aware UTC")
    seconds = int(timeframe_delta(timeframe).total_seconds())
    epoch_seconds = int(instant.timestamp())
    return datetime.fromtimestamp(epoch_seconds - epoch_seconds % seconds, tz=UTC)


def available_candles(candles: Iterable[Candle], decision_time: datetime) -> tuple[Candle, ...]:
    """Return only source-closed candles known by ``decision_time``.

    The returned sequence is chronological and therefore safe for feature
    calculation without reading a candle whose close lies in the future.
    """
    if decision_time.tzinfo is None or decision_time.utcoffset() != timedelta(0):
        raise ValueError("decision_time must be timezone-aware UTC")
    return tuple(
        sorted(
            (
                candle
                for candle in candles
                if candle.is_closed and candle.close_time <= decision_time
            ),
            key=lambda candle: candle.open_time,
        )
    )


def parse_timeframe(value: str) -> Timeframe:
    """Validate an external timeframe string."""
    if value not in _DURATIONS:
        raise ValueError(f"unsupported timeframe: {value}")
    return value
