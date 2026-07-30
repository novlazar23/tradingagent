"""Golden positive/negative fixtures and no-repaint availability contract."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradingagent.domain import Candle
from tradingagent.features import PatternConfig, PatternEngine

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "pattern_golden.json").read_text())


def candle(index: int, values: tuple[str, str, str, str] | list[str]) -> Candle:
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * index)
    return Candle(
        source="golden",
        dataset_id="patterns-v1",
        symbol="BTC/USDT",
        timeframe="15m",
        open_time=opened,
        close_time=opened + timedelta(minutes=15),
        open=Decimal(values[0]),
        high=Decimal(values[1]),
        low=Decimal(values[2]),
        close=Decimal(values[3]),
        volume=Decimal("100"),
        is_closed=True,
        source_fingerprint=f"golden-{index}",
        ingested_at=opened,
    )


@pytest.mark.parametrize("name", GOLDEN["candlesticks"])
def test_candlestick_positive_negative_and_availability_golden(name: str) -> None:
    bars = [candle(index, values) for index, values in enumerate(GOLDEN["candlesticks"][name])]
    engine = PatternEngine(PatternConfig(volume_confirmation_ratio=Decimal("0")))

    positive = engine.detect(bars, bars[-1].close_time)
    negative = engine.detect(bars, bars[-1].close_time - timedelta(microseconds=1))

    match = next(item for item in positive if item.name == name)
    assert match.available_at == bars[-1].close_time
    assert match.available_at <= bars[-1].close_time
    assert name not in {item.name for item in negative}


def chart_bars(name: str) -> list[Candle]:
    if name in {"double_bottom", "horizontal_support"}:
        values = [
            ("12", "13", "11", "12"),
            ("10", "11", "8", "9"),
            ("10", "13", "9", "12"),
            ("10", "11", "8.1", "9"),
            ("11", "14", "10", "14"),
        ]
    elif name in {"double_top", "horizontal_resistance"}:
        values = [
            ("10", "11", "9", "10"),
            ("11", "14", "10", "13"),
            ("12", "13", "8", "9"),
            ("11", "13.9", "10", "13"),
            ("9", "10", "7", "7"),
        ]
    elif name == "head_and_shoulders":
        values = [
            ("10", "11", "9", "10"),
            ("11", "14", "10", "13"),
            ("11", "12", "9", "10"),
            ("12", "17", "11", "16"),
            ("11", "12", "9", "10"),
            ("11", "14.1", "10", "13"),
            ("8", "9", "7", "7"),
        ]
    elif name == "inverse_head_and_shoulders":
        values = [
            ("12", "13", "11", "12"),
            ("10", "12", "8", "9"),
            ("11", "14", "10", "13"),
            ("8", "10", "5", "6"),
            ("11", "14", "10", "13"),
            ("10", "12", "8.1", "9"),
            ("15", "16", "14", "16"),
        ]
    else:
        high2, low2, close = {
            "symmetrical_triangle": ("13", "10", "14"),
            "ascending_triangle": ("15", "10", "16"),
            "descending_triangle": ("13", "8", "7"),
        }[name]
        values = [
            ("11", "12", "10", "11"),
            ("13", "15", "12", "14"),
            ("10", "11", "8", "9"),
            ("12", high2, "11", "12"),
            ("11", "12", low2, "11"),
            (close, str(Decimal(close) + 1), str(Decimal(close) - 1), close),
        ]
        if name == "descending_triangle":
            values.insert(-1, ("10", "12", "9", "10"))
    return [candle(index, value) for index, value in enumerate(values)]


@pytest.mark.parametrize("name", GOLDEN["chart_patterns"])
def test_chart_pattern_positive_negative_and_no_repaint_golden(name: str) -> None:
    bars = chart_bars(name)
    engine = PatternEngine(
        PatternConfig(
            pivot_window=1,
            minimum_separation=2,
            similar_level_tolerance=Decimal("0.03"),
            breakout_tolerance=Decimal("0"),
            volume_confirmation_ratio=Decimal("0"),
        )
    )
    positive = engine.detect(bars, bars[-1].close_time)
    match = next(item for item in positive if item.name == name)

    assert match.confirmed is True
    assert match.available_at <= bars[-1].close_time
    before_available = engine.detect(bars, match.available_at - timedelta(microseconds=1))
    assert name not in {item.name for item in before_available}
