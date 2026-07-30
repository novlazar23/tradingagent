"""Adapters for the isolated Freqtrade backtesting container."""

import json
from collections.abc import Iterable
from pathlib import Path

from tradingagent.domain.models import Candle


def export_ohlcv_json(directory: Path, candles: Iterable[Candle]) -> Path:
    """Write Freqtrade's plain JSON OHLCV format for one BTC/USDT timeframe."""
    values = tuple(candles)
    if not values:
        raise ValueError("at least one candle is required")
    first = values[0]
    if any(
        candle.symbol != first.symbol or candle.timeframe != first.timeframe for candle in values
    ):
        raise ValueError("all candles must use the same symbol and timeframe")
    ordered = sorted(values, key=lambda candle: candle.open_time)
    rows = [
        [
            int(candle.open_time.timestamp() * 1000),
            float(candle.open),
            float(candle.high),
            float(candle.low),
            float(candle.close),
            float(candle.volume),
        ]
        for candle in ordered
    ]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{first.symbol.replace('/', '_')}-{first.timeframe}.json"
    path.write_text(json.dumps(rows, separators=(",", ":")) + "\n", encoding="utf-8")
    return path
