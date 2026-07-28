from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tradingagent.backtest import BacktestConfig, BacktestEngine
from tradingagent.config import CostConfig, RiskConfig, StrategyConfig
from tradingagent.domain import Candle
from tradingagent.persistence.handlers import (
    _paper_execution_rows,
    _persist_candle_batch,
    _strategy_request,
)
from tradingagent.persistence.models import Base, CandleRecord, CandleRevision, DataGap
from tradingagent.trading import ExecutionModel, RiskEngine, StrategyEngine, TradingPipeline

START = datetime(2026, 1, 1, tzinfo=UTC)
Timeframe = Literal["15m", "1h", "4h", "1d"]
STEPS: dict[Timeframe, int] = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def strategy() -> StrategyConfig:
    return StrategyConfig(
        version="fixture-v1",
        timeframe_weights={"15m": Decimal(1), "1h": Decimal(1), "4h": Decimal(1), "1d": Decimal(1)},
        indicator_parameters={
            "sma_period": Decimal(2),
            "ema_period": Decimal(2),
            "rsi_period": Decimal(2),
            "macd_fast_period": Decimal(2),
            "macd_slow_period": Decimal(3),
            "macd_signal_period": Decimal(2),
            "bollinger_period": Decimal(2),
            "bollinger_stddev": Decimal(2),
            "atr_period": Decimal(2),
            "volume_period": Decimal(2),
            "rsi_oversold": Decimal(30),
            "rsi_overbought": Decimal(70),
        },
        pattern_parameters={},
        entry_threshold=Decimal("-1"),
        exit_threshold=Decimal("-1"),
        minimum_confidence=Decimal(0),
        minimum_confirming_groups=1,
        cooldown_seconds=0,
        higher_timeframe_mode="weighted",
    )


def candles(timeframe: Timeframe, count: int = 8) -> tuple[Candle, ...]:
    step = timedelta(minutes=STEPS[timeframe])
    first = START if timeframe == "15m" else START - count * step
    return tuple(
        Candle(
            "octobot",
            "dataset",
            "BTC/USDT",
            timeframe,
            first + index * step,
            first + (index + 1) * step,
            Decimal(100 + index),
            Decimal(102 + index),
            Decimal(99 + index),
            Decimal(101 + index),
            Decimal(1000 + index),
            True,
            f"{timeframe}-{index}",
            first + (index + 1) * step,
        )
        for index in range(count)
    )


def records(all_candles: dict[str, tuple[Candle, ...]]) -> list[CandleRecord]:
    result = []
    for timeframe, values in all_candles.items():
        for index, candle in enumerate(values):
            result.append(
                CandleRecord(
                    id=f"{timeframe}-{index}",
                    source=candle.source,
                    dataset_id="dataset",
                    symbol=candle.symbol,
                    timeframe=candle.timeframe,
                    open_time=candle.open_time,
                    close_time=candle.close_time,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                    is_closed=True,
                    source_fingerprint=candle.source_fingerprint,
                    ingested_at=candle.ingested_at,
                )
            )
    return result


def test_production_request_uses_all_timeframes_and_drives_a_real_fill() -> None:
    by_timeframe: dict[str, tuple[Candle, ...]] = {
        timeframe: candles(timeframe) for timeframe in STEPS
    }
    rows = records(by_timeframe)
    costs = CostConfig(
        maker_fee_rate=Decimal(0),
        taker_fee_rate=Decimal("0.001"),
        spread_bps=Decimal(0),
        slippage_model="fixed_bps",
        slippage_bps=Decimal(0),
        atr_slippage_multiplier=None,
        price_quantum=Decimal("0.01"),
        quantity_quantum=Decimal("0.0001"),
        minimum_order_value=Decimal(10),
        rounding_mode="ROUND_DOWN",
    )
    risk = RiskConfig(
        initial_capital=Decimal(10000),
        maximum_position_fraction=Decimal("0.5"),
        risk_per_trade_fraction=Decimal("0.01"),
        stop_mode="percent",
        stop_distance=Decimal("0.02"),
        take_profit_distance=Decimal("0.04"),
        maximum_daily_loss_fraction=Decimal("0.03"),
        maximum_session_drawdown_fraction=Decimal("0.1"),
        maximum_entries_per_utc_day=3,
        cooldown_seconds=0,
        minimum_cash_reserve_fraction=Decimal("0.1"),
    )
    execution = ExecutionModel(costs)
    pipeline = TradingPipeline(StrategyEngine(), RiskEngine(risk), execution)
    engine = BacktestEngine(
        execution_model=execution,
        config=BacktestConfig(initial_capital=Decimal(10000), strategy_version="fixture"),
        pipeline=pipeline,
    )

    report = engine.run(
        by_timeframe["15m"],
        lambda history, positioned: _strategy_request(
            strategy=strategy(),
            candles_by_timeframe=by_timeframe,
            rows=rows,
            decision_time=history[-1].close_time,
            has_position=positioned,
            gaps=[],
        ),
    )

    assert report.fills
    request = _strategy_request(
        strategy=strategy(),
        candles_by_timeframe=by_timeframe,
        rows=rows,
        decision_time=by_timeframe["15m"][-1].close_time,
        has_position=False,
        gaps=[],
    )
    assert {item.timeframe for item in request.contributions} == set(STEPS)
    assert {item.group for item in request.contributions} >= {"trend", "momentum"}
    assert request.group_weights["pattern"] == 1


def test_production_request_fails_closed_when_persisted_gap_is_visible() -> None:
    by_timeframe: dict[str, tuple[Candle, ...]] = {
        timeframe: candles(timeframe) for timeframe in STEPS
    }
    decision_time = by_timeframe["15m"][-1].close_time
    gap = DataGap(
        id="gap",
        dataset_id="dataset",
        timeframe="1h",
        start_time=decision_time - timedelta(hours=2),
        end_time=decision_time - timedelta(hours=1),
        reason="missing_source_candle",
        detected_at=decision_time,
    )

    request = _strategy_request(
        strategy=strategy(),
        candles_by_timeframe=by_timeframe,
        rows=records(by_timeframe),
        decision_time=decision_time,
        has_position=False,
        gaps=[gap],
    )

    decision = StrategyEngine().decide(request)
    assert request.data_blockers == (
        f"data gap 1h {(decision_time - timedelta(hours=2)).isoformat()}",
    )
    assert decision.action.value == "BLOCKED"


def test_paper_execution_rows_are_every_successor_after_checkpoint_in_order() -> None:
    rows = records({"15m": candles("15m", 5)})
    checkpoint_close = rows[1].close_time

    pending = _paper_execution_rows(rows, checkpoint_close)

    assert [row.id for row in pending] == ["15m-2", "15m-3", "15m-4"]


def test_paper_execution_rows_are_empty_when_checkpoint_is_caught_up() -> None:
    rows = records({"15m": candles("15m", 3)})

    assert _paper_execution_rows(rows, rows[-1].close_time) == []


def test_candle_batch_insert_and_revision_are_bounded_and_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    original = candles("15m", 1)[0]
    changed = replace(
        original,
        close=Decimal("101.5"),
        source_fingerprint="revised-fingerprint",
    )

    with Session(engine) as db:
        assert _persist_candle_batch(db, [original], "dataset", START) == (1, 0)
        db.commit()
        assert _persist_candle_batch(db, [original], "dataset", START) == (0, 0)
        assert _persist_candle_batch(db, [changed], "dataset", START) == (0, 1)
        db.commit()
        assert db.scalar(select(CandleRecord.close)) == Decimal("101.5")
        assert db.scalar(select(CandleRevision.id)) is not None
