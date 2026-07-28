from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradingagent.config import CostConfig, RiskConfig
from tradingagent.domain.models import Portfolio
from tradingagent.trading.execution import ExecutionModel, OrderSide
from tradingagent.trading.ledger import AtomicLedger, LedgerInvariantError
from tradingagent.trading.risk import RiskEngine, SessionRiskState
from tradingagent.trading.strategy import (
    DecisionAction,
    SignalContribution,
    StrategyEngine,
    StrategyRequest,
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def contribution(
    name: str,
    score: str,
    *,
    group: str = "trend",
    confidence: str = "0.9",
) -> SignalContribution:
    return SignalContribution(
        feature_id=name,
        feature_version="feature-v1",
        candle_ids=(f"candle-{name}",),
        group=group,
        timeframe="15m",
        score=Decimal(score),
        confidence=Decimal(confidence),
        reason=f"{name} evidence",
    )


def request(**overrides: object) -> StrategyRequest:
    values: dict[str, object] = {
        "decision_time": NOW,
        "strategy_version": "strategy-v7",
        "contributions": (
            contribution("ema", "0.9"),
            contribution("rsi", "0.8", group="momentum"),
        ),
        "group_weights": {"trend": Decimal("1"), "momentum": Decimal("1")},
        "timeframe_weights": {"15m": Decimal("1")},
        "entry_threshold": Decimal("0.7"),
        "exit_threshold": Decimal("-0.7"),
        "minimum_confidence": Decimal("0.5"),
        "minimum_confirming_groups": 2,
        "has_position": False,
    }
    values.update(overrides)
    return StrategyRequest(**values)


def costs(**overrides: object) -> CostConfig:
    values: dict[str, object] = {
        "maker_fee_rate": Decimal("0.0005"),
        "taker_fee_rate": Decimal("0.001"),
        "spread_bps": Decimal("10"),
        "slippage_model": "fixed_bps",
        "slippage_bps": Decimal("20"),
        "atr_slippage_multiplier": None,
        "price_quantum": Decimal("0.01"),
        "quantity_quantum": Decimal("0.0001"),
        "minimum_order_value": Decimal("10"),
        "rounding_mode": "ROUND_DOWN",
    }
    values.update(overrides)
    return CostConfig(**values)


def risks(**overrides: object) -> RiskConfig:
    values: dict[str, object] = {
        "initial_capital": Decimal("10000"),
        "maximum_position_fraction": Decimal("0.5"),
        "risk_per_trade_fraction": Decimal("0.01"),
        "stop_mode": "percent",
        "stop_distance": Decimal("0.02"),
        "take_profit_distance": Decimal("0.04"),
        "maximum_daily_loss_fraction": Decimal("0.03"),
        "maximum_session_drawdown_fraction": Decimal("0.10"),
        "maximum_entries_per_utc_day": 3,
        "cooldown_seconds": 900,
        "minimum_cash_reserve_fraction": Decimal("0.10"),
    }
    values.update(overrides)
    return RiskConfig(**values)


@pytest.mark.parametrize(
    ("overrides", "expected", "reason"),
    [
        ({"data_blockers": ("gap",)}, DecisionAction.BLOCKED, "gap"),
        (
            {"forced_exit_reasons": ("stop_loss",), "has_position": True},
            DecisionAction.EXIT_LONG,
            "stop_loss",
        ),
        (
            {
                "contributions": (
                    contribution("entry", "1", group="entry"),
                    contribution("exit", "-1", group="exit"),
                ),
                "group_weights": {"entry": Decimal("1"), "exit": Decimal("1")},
                "minimum_confirming_groups": 1,
                "entry_threshold": Decimal("0"),
                "exit_threshold": Decimal("0"),
                "has_position": True,
            },
            DecisionAction.EXIT_LONG,
            "exit priority",
        ),
        (
            {
                "contributions": (
                    contribution("bull", "0.4"),
                    contribution("bear", "-0.4", group="momentum"),
                )
            },
            DecisionAction.HOLD,
            "threshold",
        ),
        ({"cooldown_active": True}, DecisionAction.BLOCKED, "cooldown"),
    ],
)
def test_strategy_conflict_matrix_is_deterministic(
    overrides: dict[str, object],
    expected: DecisionAction,
    reason: str,
) -> None:
    decision = StrategyEngine().decide(request(**overrides))

    assert decision.action is expected
    assert reason in " ".join(decision.reasons).lower()


def test_strategy_decision_preserves_reproducible_audit_lineage() -> None:
    decision = StrategyEngine("strategy-engine-v2").decide(request())

    assert decision.action is DecisionAction.ENTER_LONG
    assert decision.strategy_version == "strategy-v7"
    assert decision.engine_version == "strategy-engine-v2"
    repeated = StrategyEngine("strategy-engine-v2").decide(request())
    assert decision.decision_id == repeated.decision_id
    assert decision.input_fingerprint
    assert decision.contributions[0].feature_version == "feature-v1"
    assert decision.contributions[0].candle_ids == ("candle-ema",)


def test_execution_model_applies_spread_slippage_rounding_and_separate_fee() -> None:
    model = ExecutionModel(costs(), version="cost-v3")

    buy = model.fill(
        side=OrderSide.BUY,
        reference_price=Decimal("100"),
        requested_quantity=Decimal("1.23456"),
        atr=None,
    )
    sell = model.fill(
        side=OrderSide.SELL,
        reference_price=Decimal("100"),
        requested_quantity=Decimal("1.23456"),
        atr=None,
    )

    assert buy.fill_price == Decimal("100.25")
    assert sell.fill_price == Decimal("99.75")
    assert buy.quantity == sell.quantity == Decimal("1.2345")
    assert buy.fee == Decimal("0.123758625")
    assert buy.execution_model_version == "cost-v3"
    assert buy.spread_cost == Decimal("0.061725")
    assert buy.slippage_cost == Decimal("0.246900")


def test_atr_scaled_slippage_uses_atr_relative_to_reference_price() -> None:
    model = ExecutionModel(
        costs(
            slippage_model="atr_scaled",
            slippage_bps=Decimal("0"),
            atr_slippage_multiplier=Decimal("0.5"),
        )
    )

    fill = model.fill(
        side=OrderSide.BUY,
        reference_price=Decimal("100"),
        requested_quantity=Decimal("1"),
        atr=Decimal("2"),
    )

    assert fill.fill_price == Decimal("101.05")


def test_risk_engine_sizes_to_smallest_limit_and_blocks_invalid_long_flat_actions() -> None:
    engine = RiskEngine(risks())
    state = SessionRiskState.initial(Decimal("10000"))

    approval = engine.approve_entry(
        portfolio=Portfolio(cash=Decimal("10000"), btc=Decimal("0")),
        state=state,
        entry_price=Decimal("100"),
        estimated_fee_rate=Decimal("0.001"),
        now=NOW,
    )

    assert approval.approved
    assert approval.quantity == Decimal("50")
    assert approval.stop_price == Decimal("98")
    assert (
        engine.approve_exit(
            portfolio=Portfolio(cash=Decimal("0"), btc=Decimal("1")),
            requested_quantity=Decimal("2"),
        ).approved
        is False
    )
    assert (
        engine.approve_entry(
            portfolio=Portfolio(cash=Decimal("0"), btc=Decimal("1")),
            state=state,
            entry_price=Decimal("100"),
            estimated_fee_rate=Decimal("0.001"),
            now=NOW,
        ).approved
        is False
    )


def test_drawdown_kill_switch_stays_paused_until_explicit_resume() -> None:
    engine = RiskEngine(risks(maximum_session_drawdown_fraction=Decimal("0.10")))
    state = SessionRiskState.initial(Decimal("10000"))

    paused = engine.observe_equity(state, Decimal("8900"), NOW)

    assert paused.paused
    assert paused.audit_events[-1].event_type == "drawdown_kill_switch"
    assert not engine.approve_entry(
        portfolio=Portfolio(cash=Decimal("8900"), btc=Decimal("0")),
        state=paused,
        entry_price=Decimal("100"),
        estimated_fee_rate=Decimal("0"),
        now=NOW,
    ).approved
    assert engine.observe_equity(paused, Decimal("11000"), NOW).paused
    assert engine.resume(paused, actor="admin").paused is False


def test_risk_state_enforces_daily_loss_entry_limit_and_cooldown() -> None:
    engine = RiskEngine(risks(maximum_entries_per_utc_day=1, cooldown_seconds=900))
    state = SessionRiskState.initial(Decimal("10000"))
    state = engine.observe_equity(state, Decimal("10000"), NOW)
    state = engine.record_entry(state, NOW)

    limited = engine.approve_entry(
        portfolio=Portfolio(cash=Decimal("10000"), btc=Decimal("0")),
        state=state,
        entry_price=Decimal("100"),
        estimated_fee_rate=Decimal("0"),
        now=NOW,
    )
    assert not limited.approved
    assert "daily entry limit" in " ".join(limited.reasons)

    exited = engine.record_exit(SessionRiskState.initial(Decimal("10000")), NOW)
    cooldown = engine.approve_entry(
        portfolio=Portfolio(cash=Decimal("10000"), btc=Decimal("0")),
        state=exited,
        entry_price=Decimal("100"),
        estimated_fee_rate=Decimal("0"),
        now=NOW,
    )
    assert not cooldown.approved
    assert "cooldown" in " ".join(cooldown.reasons)

    daily_loss = engine.observe_equity(state, Decimal("9600"), NOW)
    assert daily_loss.paused
    assert daily_loss.pause_reason == "maximum daily loss exceeded"


def test_atomic_ledger_fill_preserves_decimal_balances_and_rolls_back_on_error() -> None:
    ledger = AtomicLedger(Portfolio(cash=Decimal("1000"), btc=Decimal("0")))
    fill = ExecutionModel(costs()).fill(
        side=OrderSide.BUY,
        reference_price=Decimal("100"),
        requested_quantity=Decimal("1"),
        atr=None,
        fill_id="fill-1",
        decision_id="decision-1",
        risk_check_id="risk-1",
    )

    ledger.apply_fill(fill, occurred_at=NOW)
    snapshot = ledger.snapshot()

    assert snapshot.portfolio.cash == Decimal("899.64975")
    assert snapshot.portfolio.btc == Decimal("1")
    assert len(snapshot.entries) == 3
    assert snapshot.entries[-1].entry_type == "fee"
    assert snapshot.fills[0].decision_id == "decision-1"

    impossible = ExecutionModel(costs()).fill(
        side=OrderSide.SELL,
        reference_price=Decimal("100"),
        requested_quantity=Decimal("2"),
        atr=None,
    )
    with pytest.raises(LedgerInvariantError):
        ledger.apply_fill(impossible, occurred_at=NOW)

    assert ledger.snapshot() == snapshot
