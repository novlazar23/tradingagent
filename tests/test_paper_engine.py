from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradingagent.config import CostConfig, RiskConfig
from tradingagent.domain.models import Portfolio
from tradingagent.paper import (
    CandleHealth,
    InMemoryPaperRepository,
    PaperCycle,
    PaperEngine,
    PaperSessionState,
    SessionStatus,
)
from tradingagent.trading.execution import ExecutionModel
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
        reference_price=Decimal("100"),
        atr=Decimal("2"),
        strategy_request=strategy_request(),
        health=replace(health, **health_overrides),
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


def test_repeated_candle_after_restart_does_not_duplicate_order_fill_or_ledger() -> None:
    repository = InMemoryPaperRepository()
    running_session(repository)

    first = engine(repository).process("paper-1", cycle())
    restarted = engine(repository).process("paper-1", cycle())

    assert restarted == first
    assert len(repository.decisions("paper-1")) == 1
    assert len(repository.orders("paper-1")) == 1
    assert len(repository.fills("paper-1")) == 1
    assert len(repository.get("paper-1").ledger.entries) == 3


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
