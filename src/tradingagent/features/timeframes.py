"""No-look-ahead candle alignment helpers."""

from collections.abc import Iterable, Mapping
from datetime import datetime

from tradingagent.domain import Candle


def align_closed_candles(candles: Iterable[Candle], decision_time: datetime) -> tuple[Candle, ...]:
    """Return closed candles observable at ``decision_time`` in event-time order."""
    return tuple(
        sorted(
            (
                candle
                for candle in candles
                if candle.is_closed and candle.close_time <= decision_time
            ),
            key=lambda candle: candle.close_time,
        ),
    )


def organize_timeframes(
    candles: Iterable[Candle], decision_time: datetime
) -> Mapping[str, tuple[Candle, ...]]:
    """Group observable native candles into the four supported timeframes."""
    result: dict[str, tuple[Candle, ...]] = {}
    materialized = tuple(candles)
    for timeframe in ("15m", "1h", "4h", "1d"):
        result[timeframe] = align_closed_candles(
            (candle for candle in materialized if candle.timeframe == timeframe),
            decision_time,
        )
    return result
