"""Property and clock-boundary acceptance tests for financial invariants."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import time_machine
from hypothesis import given
from hypothesis import strategies as st

from tradingagent.config import CostConfig, RiskConfig
from tradingagent.domain.models import Portfolio
from tradingagent.trading.execution import ExecutionModel, OrderSide
from tradingagent.trading.ledger import AtomicLedger, LedgerInvariantError
from tradingagent.trading.risk import RiskEngine, SessionRiskState

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def execution() -> ExecutionModel:
    return ExecutionModel(
        CostConfig(
            maker_fee_rate=Decimal("0"),
            taker_fee_rate=Decimal("0.001"),
            spread_bps=Decimal("2"),
            slippage_model="fixed_bps",
            slippage_bps=Decimal("3"),
            atr_slippage_multiplier=None,
            price_quantum=Decimal("0.01"),
            quantity_quantum=Decimal("0.0001"),
            minimum_order_value=Decimal("1"),
            rounding_mode="ROUND_DOWN",
        )
    )


def risk() -> RiskEngine:
    return RiskEngine(
        RiskConfig(
            initial_capital=Decimal("10000"),
            maximum_position_fraction=Decimal("0.8"),
            risk_per_trade_fraction=Decimal("0.02"),
            stop_mode="percent",
            stop_distance=Decimal("0.02"),
            take_profit_distance=Decimal("0.04"),
            maximum_daily_loss_fraction=Decimal("0.03"),
            maximum_session_drawdown_fraction=Decimal("0.10"),
            maximum_entries_per_utc_day=3,
            cooldown_seconds=900,
            minimum_cash_reserve_fraction=Decimal("0.10"),
        )
    )


positive_amounts = st.integers(min_value=100, max_value=1_000_000).map(
    lambda value: Decimal(value) / 100
)


@given(cash=positive_amounts, price=st.integers(min_value=10, max_value=100_000))
def test_risk_approved_entry_is_long_flat_and_affordable(cash: Decimal, price: int) -> None:
    """Every approved quantity fits cash including modeled execution costs."""
    model = execution()
    portfolio = Portfolio(cash=cash, btc=Decimal(0))
    approval = risk().approve_entry(
        portfolio=portfolio,
        state=SessionRiskState.initial(cash),
        entry_price=Decimal(price),
        estimated_fee_rate=Decimal("0.001"),
        execution_model=model,
        now=NOW,
    )
    if not approval.approved:
        assert approval.quantity == 0
        return

    fill = model.fill(
        side=OrderSide.BUY,
        reference_price=Decimal(price),
        requested_quantity=approval.quantity,
        atr=None,
    )
    snapshot = AtomicLedger(portfolio).apply_fill(fill, occurred_at=NOW)
    assert snapshot.portfolio.cash >= 0
    assert snapshot.portfolio.btc == fill.quantity > 0


@given(held=positive_amounts, excess=positive_amounts)
def test_ledger_rejects_oversell_atomically(held: Decimal, excess: Decimal) -> None:
    """An invalid sale never changes balances, entries, or fill history."""
    ledger = AtomicLedger(Portfolio(cash=Decimal("1000"), btc=held))
    before = ledger.snapshot()
    fill = execution().fill(
        side=OrderSide.SELL,
        reference_price=Decimal("100"),
        requested_quantity=held + excess,
        atr=None,
    )

    with pytest.raises(LedgerInvariantError, match="exceeds"):
        ledger.apply_fill(fill, occurred_at=NOW)
    assert ledger.snapshot() == before


@time_machine.travel(NOW, tick=False)
def test_risk_resume_uses_frozen_utc_system_clock() -> None:
    state = risk().observe_equity(
        SessionRiskState.initial(Decimal("10000")),
        Decimal("8000"),
        NOW,
    )

    resumed = risk().resume(state, actor="risk-admin")

    assert resumed.audit_events[-1].occurred_at == NOW
    assert resumed.audit_events[-1].occurred_at.tzinfo is UTC


def test_risk_rejects_local_or_naive_clock_values() -> None:
    with pytest.raises(ValueError, match="UTC"):
        risk().observe_equity(
            SessionRiskState.initial(Decimal("10000")),
            Decimal("10000"),
            datetime(2026, 7, 28, 12),
        )
