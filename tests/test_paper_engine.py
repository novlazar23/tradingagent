from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool

from tradingagent.backtest import BacktestConfig, BacktestEngine
from tradingagent.config import CostConfig, RiskConfig
from tradingagent.domain.models import Candle, Portfolio
from tradingagent.paper import (
    CandleHealth,
    InMemoryPaperRepository,
    PaperCycle,
    PaperEngine,
    PaperSessionState,
    SessionStatus,
)
from tradingagent.paper.repository import SQLAlchemyPaperRepository
from tradingagent.persistence.models import (
    Base,
    FillRecord,
    OrderRecord,
    SignalDecisionRecord,
)
from tradingagent.trading.execution import ExecutionModel
from tradingagent.trading.pipeline import TradingPipeline
from tradingagent.trading.risk import RiskEngine, SessionRiskState
from tradingagent.trading.strategy import (
    SignalContribution,
    StrategyEngine,
    StrategyRequest,
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def costs() -> CostConfig:
    return CostConfig(
        maker_fee_rate=Decimal("0"),
        taker_fee_rate=Decimal("0.001"),
        spread_bps=Decimal("0"),
        slippage_model="fixed_bps",
        slippage_bps=Decimal("0"),
        atr_slippage_multiplier=None,
        price_quantum=Decimal("0.01"),
        quantity_quantum=Decimal("0.0001"),
        minimum_order_value=Decimal("10"),
        rounding_mode="ROUND_DOWN",
    )


def risks() -> RiskConfig:
    return RiskConfig(
        initial_capital=Decimal("10000"),
        maximum_position_fraction=Decimal("0.5"),
        risk_per_trade_fraction=Decimal("0.01"),
        stop_mode="percent",
        stop_distance=Decimal("0.02"),
        take_profit_distance=Decimal("0.04"),
        maximum_daily_loss_fraction=Decimal("0.03"),
        maximum_session_drawdown_fraction=Decimal("0.10"),
        maximum_entries_per_utc_day=3,
        cooldown_seconds=0,
        minimum_cash_reserve_fraction=Decimal("0.10"),
    )


def strategy_request(*, has_position: bool = False) -> StrategyRequest:
    contributions = (
        SignalContribution(
            feature_id="ema",
            feature_version="v1",
            candle_ids=("15m-1",),
            group="trend",
            timeframe="15m",
            score=Decimal("0.9"),
            confidence=Decimal("0.9"),
            reason="bullish",
        ),
        SignalContribution(
            feature_id="rsi",
            feature_version="v1",
            candle_ids=("15m-1",),
            group="momentum",
            timeframe="15m",
            score=Decimal("0.8"),
            confidence=Decimal("0.9"),
            reason="confirmed",
        ),
    )
    return StrategyRequest(
        decision_time=NOW,
        strategy_version="strategy-v1",
        contributions=contributions,
        group_weights={"trend": Decimal("1"), "momentum": Decimal("1")},
        timeframe_weights={"15m": Decimal("1")},
        entry_threshold=Decimal("0.7"),
        exit_threshold=Decimal("-0.7"),
        minimum_confidence=Decimal("0.5"),
        minimum_confirming_groups=2,
        has_position=has_position,
    )


def engine(repository: InMemoryPaperRepository) -> PaperEngine:
    return PaperEngine(
        repository=repository,
        strategy=StrategyEngine(),
        risk=RiskEngine(risks()),
        execution=ExecutionModel(costs()),
        maximum_candle_age=timedelta(minutes=20),
    )


def running_session(repository: InMemoryPaperRepository) -> PaperSessionState:
    created = repository.create_session(
        session_id="paper-1",
        portfolio=Portfolio(cash=Decimal("10000"), btc=Decimal("0")),
        risk_state=SessionRiskState.initial(Decimal("10000")),
        configuration_versions=("strategy-v1", "cost-v1", "risk-v1"),
    )
    return engine(repository).start(created.session_id, now=NOW)


def cycle(**health_overrides: object) -> PaperCycle:
    health = CandleHealth(
        source_available=True,
        has_gap=False,
        clock_drift=False,
        ledger_consistent=True,
    )
    return PaperCycle(
        candle_id="15m-1",
        candle_close_time=NOW,
        observed_at=NOW + timedelta(minutes=1),
        candle_open=Decimal("100"),
        reference_price=Decimal("100"),
        candle_low=Decimal("99"),
        candle_high=Decimal("101"),
        atr=Decimal("2"),
        strategy_request=strategy_request(),
        health=replace(health, **health_overrides),
    )


def test_cycle_rejects_future_observation_and_mismatched_decision_time() -> None:
    with pytest.raises(ValueError, match="observed"):
        replace(cycle(), observed_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError, match="decision time"):
        replace(
            cycle(),
            strategy_request=replace(strategy_request(), decision_time=NOW + timedelta(seconds=1)),
        )


def test_session_lifecycle_requires_explicit_transitions_and_stop_is_terminal() -> None:
    repository = InMemoryPaperRepository()
    service = engine(repository)
    session = repository.create_session(
        session_id="paper-1",
        portfolio=Portfolio(cash=Decimal("10000"), btc=Decimal("0")),
        risk_state=SessionRiskState.initial(Decimal("10000")),
        configuration_versions=("strategy-v1", "cost-v1", "risk-v1"),
    )

    assert session.status is SessionStatus.CREATED
    assert service.start("paper-1", now=NOW).status is SessionStatus.RUNNING
    paused = service.pause("paper-1", actor="operator", reason="maintenance", now=NOW)
    assert paused.status is SessionStatus.PAUSED
    assert service.resume("paper-1", actor="operator", now=NOW).status is SessionStatus.RUNNING
    assert service.stop("paper-1", actor="operator", now=NOW).status is SessionStatus.STOPPED
    with pytest.raises(ValueError, match="terminal"):
        service.start("paper-1", now=NOW)


def test_decision_is_persisted_as_intent_and_fills_at_next_candle_open() -> None:
    repository = InMemoryPaperRepository()
    running_session(repository)

    first = engine(repository).process("paper-1", cycle())
    assert repository.orders("paper-1") == ()
    assert repository.fills("paper-1") == ()
    assert first.pending_intent is not None

    next_cycle = replace(
        cycle(),
        candle_id="15m-2",
        candle_close_time=NOW + timedelta(minutes=15),
        observed_at=NOW + timedelta(minutes=16),
        candle_open=Decimal("101"),
        reference_price=Decimal("101"),
        candle_low=Decimal("100"),
        candle_high=Decimal("102"),
        strategy_request=replace(strategy_request(), decision_time=NOW + timedelta(minutes=15)),
    )
    restarted = engine(repository).process("paper-1", next_cycle)

    assert restarted.pending_intent is None
    assert len(repository.decisions("paper-1")) == 2
    assert len(repository.orders("paper-1")) == 1
    assert len(repository.fills("paper-1")) == 1
    assert repository.fills("paper-1")[0].reference_price == Decimal("101")
    assert len(repository.get("paper-1").ledger.entries) == 3


def test_repeated_candle_after_restart_does_not_duplicate_decision() -> None:
    repository = InMemoryPaperRepository()
    running_session(repository)

    first = engine(repository).process("paper-1", cycle())
    restarted = engine(repository).process("paper-1", cycle())

    assert restarted == first
    assert len(repository.decisions("paper-1")) == 1


@pytest.mark.parametrize("failure_stage", ["after_decision", "after_order", "after_fill"])
def test_sql_candle_commit_rolls_back_and_restart_creates_no_duplicates(
    failure_stage: str,
) -> None:
    database = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(database)
    initial = SQLAlchemyPaperRepository(database)
    initial.create_session(
        session_id="paper-1",
        portfolio=Portfolio(Decimal("10000"), Decimal("0")),
        risk_state=SessionRiskState.initial(Decimal("10000")),
        configuration_versions=("strategy-v1", "cost-v1", "risk-v1"),
    )
    paper_engine = PaperEngine(
        repository=initial,
        strategy=StrategyEngine(),
        risk=RiskEngine(risks()),
        execution=ExecutionModel(costs()),
        maximum_candle_age=timedelta(minutes=20),
    )
    paper_engine.start("paper-1", now=NOW)
    paper_engine.process("paper-1", cycle())
    second = replace(
        cycle(),
        candle_id="15m-2",
        candle_close_time=NOW + timedelta(minutes=15),
        observed_at=NOW + timedelta(minutes=16),
        candle_open=Decimal("101"),
        reference_price=Decimal("101"),
        candle_low=Decimal("100"),
        candle_high=Decimal("102"),
        strategy_request=replace(strategy_request(), decision_time=NOW + timedelta(minutes=15)),
    )

    def fail(stage: str) -> None:
        if stage == failure_stage:
            raise RuntimeError(f"injected {stage}")

    failing = SQLAlchemyPaperRepository(database, failure_injector=fail)
    with pytest.raises(RuntimeError, match="injected"):
        PaperEngine(
            repository=failing,
            strategy=StrategyEngine(),
            risk=RiskEngine(risks()),
            execution=ExecutionModel(costs()),
            maximum_candle_age=timedelta(minutes=20),
        ).process("paper-1", second)

    with failing.sessions() as db:
        assert db.scalar(select(func.count()).select_from(SignalDecisionRecord)) == 1
        assert db.scalar(select(func.count()).select_from(OrderRecord)) == 0
        assert db.scalar(select(func.count()).select_from(FillRecord)) == 0
    recovered = SQLAlchemyPaperRepository(database)
    result = PaperEngine(
        repository=recovered,
        strategy=StrategyEngine(),
        risk=RiskEngine(risks()),
        execution=ExecutionModel(costs()),
        maximum_candle_age=timedelta(minutes=20),
    ).process("paper-1", second)
    assert result.pending_intent is None
    with recovered.sessions() as db:
        assert db.scalar(select(func.count()).select_from(SignalDecisionRecord)) == 2
        assert db.scalar(select(func.count()).select_from(OrderRecord)) == 1
        assert db.scalar(select(func.count()).select_from(FillRecord)) == 1


@pytest.mark.parametrize(
    ("health_overrides", "expected"),
    [
        ({"source_available": False}, SessionStatus.DEGRADED),
        ({"has_gap": True}, SessionStatus.DEGRADED),
        ({"clock_drift": True}, SessionStatus.PAUSED),
        ({"ledger_consistent": False}, SessionStatus.FAILED),
    ],
)
def test_unhealthy_inputs_fail_closed_and_block_new_entries(
    health_overrides: dict[str, object], expected: SessionStatus
) -> None:
    repository = InMemoryPaperRepository()
    running_session(repository)

    result = engine(repository).process("paper-1", cycle(**health_overrides))

    assert result.status is expected
    assert repository.orders("paper-1") == ()
    assert repository.fills("paper-1") == ()


def test_stale_candle_degrades_session_and_blocks_entry() -> None:
    repository = InMemoryPaperRepository()
    running_session(repository)
    stale = replace(cycle(), observed_at=NOW + timedelta(hours=1))

    result = engine(repository).process("paper-1", stale)

    assert result.status is SessionStatus.DEGRADED
    assert "stale" in result.last_error
    assert repository.orders("paper-1") == ()


def test_drawdown_kill_switch_is_latched_until_explicit_risk_resume() -> None:
    repository = InMemoryPaperRepository()
    session = running_session(repository)
    latched = engine(repository).risk.observe_equity(session.risk_state, Decimal("8000"), NOW)
    repository.save(replace(session, risk_state=latched))

    blocked = engine(repository).process("paper-1", cycle())

    assert blocked.status is SessionStatus.PAUSED
    assert repository.orders("paper-1") == ()
    with pytest.raises(ValueError, match="kill switch"):
        engine(repository).resume("paper-1", actor="operator", now=NOW)
    assert (
        engine(repository)
        .resume("paper-1", actor="risk-admin", now=NOW, clear_kill_switch=True)
        .status
        is SessionStatus.RUNNING
    )


def test_paper_persists_protection_and_uses_stop_conservatively() -> None:
    repository = InMemoryPaperRepository()
    running_session(repository)
    engine(repository).process("paper-1", cycle())
    entry_cycle = replace(
        cycle(),
        candle_id="15m-2",
        candle_close_time=NOW + timedelta(minutes=15),
        observed_at=NOW + timedelta(minutes=16),
        strategy_request=replace(strategy_request(), decision_time=NOW + timedelta(minutes=15)),
    )
    entered = engine(repository).process("paper-1", entry_cycle)
    assert entered.stop_price == Decimal("98")
    assert entered.take_profit_price == Decimal("104")

    next_request = replace(
        strategy_request(has_position=True),
        decision_time=NOW + timedelta(minutes=30),
    )
    protective = replace(
        cycle(),
        candle_id="15m-3",
        candle_close_time=NOW + timedelta(minutes=30),
        observed_at=NOW + timedelta(minutes=31),
        reference_price=Decimal("102"),
        candle_low=Decimal("97"),
        candle_high=Decimal("105"),
        strategy_request=next_request,
    )

    exited = engine(repository).process("paper-1", protective)

    assert exited.ledger.portfolio.btc == 0
    assert repository.fills("paper-1")[-1].reference_price == Decimal("98")
    assert exited.stop_price is None
    assert exited.take_profit_price is None


def test_paper_observes_equity_and_latches_drawdown_pause() -> None:
    repository = InMemoryPaperRepository()
    session = running_session(repository)
    repository.save(
        replace(
            session,
            ledger=replace(
                session.ledger,
                portfolio=Portfolio(cash=Decimal("0"), btc=Decimal("100")),
            ),
        )
    )
    falling = replace(
        cycle(),
        reference_price=Decimal("80"),
        candle_low=Decimal("79"),
        candle_high=Decimal("81"),
        strategy_request=strategy_request(has_position=True),
    )

    result = engine(repository).process("paper-1", falling)

    assert result.status is SessionStatus.PAUSED
    assert result.risk_state.paused


def test_backtest_and_paper_share_strategy_risk_execution_pipeline() -> None:
    repository = InMemoryPaperRepository()
    running_session(repository)
    paper = engine(repository)
    paper.process("paper-1", cycle())
    paper.process(
        "paper-1",
        replace(
            cycle(),
            candle_id="15m-2",
            candle_close_time=NOW + timedelta(minutes=15),
            observed_at=NOW + timedelta(minutes=16),
            strategy_request=replace(strategy_request(), decision_time=NOW + timedelta(minutes=15)),
        ),
    )
    paper_fill = repository.fills("paper-1")[0]

    bars = tuple(
        Candle(
            source="fixture",
            dataset_id="parity",
            symbol="BTC/USDT",
            timeframe="15m",
            open_time=NOW - timedelta(minutes=15) + timedelta(minutes=15 * index),
            close_time=NOW + timedelta(minutes=15 * index),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
            is_closed=True,
            source_fingerprint=f"parity-{index}",
            ingested_at=NOW,
        )
        for index in range(2)
    )
    execution = ExecutionModel(costs())
    pipeline = TradingPipeline(StrategyEngine(), RiskEngine(risks()), execution)
    backtest = BacktestEngine(
        execution_model=execution,
        config=BacktestConfig(initial_capital=Decimal("10000")),
        pipeline=pipeline,
    )

    result = backtest.run(
        bars,
        lambda history, has_position: (
            replace(
                strategy_request(has_position=has_position),
                decision_time=history[-1].close_time,
            )
            if len(history) == 1
            else None
        ),
    )

    assert result.fills[0].decision_id == paper_fill.decision_id
    assert result.fills[0].fill_price == paper_fill.fill_price
    assert result.fills[0].quantity == paper_fill.quantity
