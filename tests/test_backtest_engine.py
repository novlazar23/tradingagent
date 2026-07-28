from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradingagent.backtest import BacktestConfig, BacktestEngine, BacktestOrderIntent
from tradingagent.config import CostConfig
from tradingagent.domain.models import Candle
from tradingagent.trading.execution import ExecutionModel
from tradingagent.trading.strategy import DecisionAction


def candle(index: int, *, o: str, h: str, low: str, close: str) -> Candle:
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * index)
    return Candle(
        source="fixture",
        dataset_id="btc",
        symbol="BTC/USDT",
        timeframe="15m",
        open_time=opened,
        close_time=opened + timedelta(minutes=15),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
        is_closed=True,
        source_fingerprint=f"c{index}",
        ingested_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def costs() -> CostConfig:
    return CostConfig(
        maker_fee_rate=Decimal("0"),
        taker_fee_rate=Decimal("0.001"),
        spread_bps=Decimal("2"),
        slippage_model="fixed_bps",
        slippage_bps=Decimal("3"),
        atr_slippage_multiplier=None,
        price_quantum=Decimal("0.01"),
        quantity_quantum=Decimal("0.000001"),
        minimum_order_value=Decimal("1"),
        rounding_mode="ROUND_DOWN",
    )


def test_signal_executes_on_next_candle_and_same_bar_uses_stop() -> None:
    candles = (
        candle(0, o="100", h="101", low="99", close="100"),
        candle(1, o="100", h="112", low="94", close="108"),
        candle(2, o="108", h="109", low="107", close="108"),
    )

    def strategy(history: tuple[Candle, ...], has_position: bool) -> BacktestOrderIntent | None:
        if len(history) == 1:
            return BacktestOrderIntent(
                decision_id="entry",
                action=DecisionAction.ENTER_LONG,
                quantity=Decimal("1"),
                stop_price=Decimal("95"),
                take_profit_price=Decimal("110"),
            )
        return None

    result = BacktestEngine(
        execution_model=ExecutionModel(costs()),
        config=BacktestConfig(initial_capital=Decimal("1000")),
    ).run(candles, strategy)

    assert result.trades[0].entry_fill.reference_price == Decimal("100")
    assert result.trades[0].entry_fill.decision_id == "entry"
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].exit_fill.reference_price == Decimal("95")
    assert result.trades[0].exit_fill.decision_id.startswith("protective:")


def test_identical_inputs_reproduce_snapshot_trades_curve_and_metrics() -> None:
    candles = (
        candle(0, o="100", h="101", low="99", close="100"),
        candle(1, o="101", h="104", low="100", close="103"),
        candle(2, o="104", h="106", low="103", close="105"),
    )

    def strategy(history: tuple[Candle, ...], has_position: bool) -> BacktestOrderIntent | None:
        if len(history) == 1:
            return BacktestOrderIntent("buy", DecisionAction.ENTER_LONG, Decimal("1"))
        if len(history) == 2 and has_position:
            return BacktestOrderIntent("sell", DecisionAction.EXIT_LONG, Decimal("1"))
        return None

    engine = BacktestEngine(
        execution_model=ExecutionModel(costs()),
        config=BacktestConfig(initial_capital=Decimal("1000")),
    )
    first = engine.run(candles, strategy)
    second = engine.run(candles, strategy)

    assert first == second
    assert first.snapshot.data_fingerprint
    assert first.snapshot.run_fingerprint
    assert first.metrics.total_fees > 0
    assert first.metrics.total_slippage > 0
    assert first.metrics.buy_and_hold_return > 0
    assert first.metrics.realized_pnl == first.metrics.net_pnl
    assert first.metrics.unrealized_pnl == 0
    assert "overfitting" in " ".join(first.warnings).lower()
    assert "out-of-sample" in " ".join(first.warnings).lower()


def test_rejects_open_unsorted_and_mixed_timeframe_candles() -> None:
    closed = candle(0, o="100", h="101", low="99", close="100")
    open_candle = replace(closed, is_closed=False)
    engine = BacktestEngine(
        execution_model=ExecutionModel(costs()),
        config=BacktestConfig(initial_capital=Decimal("1000")),
    )
    try:
        engine.run((open_candle,), lambda *_: None)
    except ValueError as exc:
        assert "closed" in str(exc)
    else:
        raise AssertionError("open candle accepted")


def test_rejects_data_gaps_instead_of_silently_distorting_metrics() -> None:
    bars = (
        candle(0, o="100", h="101", low="99", close="100"),
        candle(2, o="100", h="101", low="99", close="100"),
    )
    engine = BacktestEngine(
        execution_model=ExecutionModel(costs()),
        config=BacktestConfig(initial_capital=Decimal("1000")),
    )
    try:
        engine.run(bars, lambda *_: None)
    except ValueError as exc:
        assert "gaps" in str(exc)
    else:
        raise AssertionError("gapped candle series accepted")
