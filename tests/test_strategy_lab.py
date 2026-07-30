from decimal import Decimal

import pytest

from tradingagent.strategy_lab.compiler import compile_freqtrade_strategy
from tradingagent.strategy_lab.models import StrategySpec, StrategyStatus, TranscriptSegment


def strategy_spec() -> StrategySpec:
    return StrategySpec(
        name="rsi_breakout",
        source_url="https://www.youtube.com/watch?v=example",
        symbol="BTC/USDT",
        timeframe="1h",
        transcript_sha256="a" * 64,
        status=StrategyStatus.APPROVED,
        indicators={"rsi": {"period": 14}},
        entry_rules=("rsi < 30", "close > ema_20"),
        exit_rules=("rsi > 70",),
        risk={"stop_loss": Decimal("0.02"), "take_profit": Decimal("0.04")},
        evidence=(
            TranscriptSegment(start_seconds=12.5, end_seconds=18.0, text="Buy below RSI 30"),
        ),
    )


def test_strategy_spec_rejects_execution_before_approval() -> None:
    with pytest.raises(ValueError, match="approved"):
        compile_freqtrade_strategy(
            strategy_spec().model_copy(update={"status": StrategyStatus.DRAFT})
        )


def test_compiler_generates_deterministic_freqtrade_strategy() -> None:
    first = compile_freqtrade_strategy(strategy_spec())
    second = compile_freqtrade_strategy(strategy_spec())

    assert first == second
    assert "class RsiBreakoutStrategy(IStrategy):" in first
    assert 'timeframe = "1h"' in first
    assert 'dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)' in first
    assert 'dataframe["ema_20"] = ta.EMA(dataframe, timeperiod=20)' in first
    assert 'minimal_roi = {"0": 0.04}' in first
    assert "stoploss = -0.02" in first
    assert "rsi_breakout" in first
    assert "# Evidence: 12.5-18.0s" in first
