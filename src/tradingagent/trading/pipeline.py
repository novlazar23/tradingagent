"""Shared strategy-to-risk planning used by every simulation mode."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradingagent.domain.models import Portfolio
from tradingagent.trading.execution import ExecutionModel
from tradingagent.trading.risk import RiskApproval, RiskEngine, SessionRiskState
from tradingagent.trading.strategy import (
    DecisionAction,
    SignalDecision,
    StrategyEngine,
    StrategyRequest,
)


@dataclass(frozen=True, slots=True)
class PipelinePlan:
    """Auditable strategy decision and its optional risk authorization."""

    decision: SignalDecision
    approval: RiskApproval | None


class TradingPipeline:
    """Single Strategy -> Risk -> Execution dependency graph for simulations."""

    def __init__(
        self, strategy: StrategyEngine, risk: RiskEngine, execution: ExecutionModel
    ) -> None:
        self.strategy = strategy
        self.risk = risk
        self.execution = execution

    def plan(
        self,
        *,
        request: StrategyRequest,
        portfolio: Portfolio,
        risk_state: SessionRiskState,
        reference_price: Decimal,
        atr: Decimal | None,
        now: datetime,
    ) -> PipelinePlan:
        decision = self.strategy.decide(request)
        approval: RiskApproval | None = None
        if decision.action is DecisionAction.ENTER_LONG:
            approval = self.risk.approve_entry(
                portfolio=portfolio,
                state=risk_state,
                entry_price=reference_price,
                estimated_fee_rate=self.execution.config.taker_fee_rate,
                execution_model=self.execution,
                now=now,
                atr=atr,
            )
        elif decision.action is DecisionAction.EXIT_LONG and portfolio.btc > 0:
            approval = self.risk.approve_exit(
                portfolio=portfolio,
                requested_quantity=portfolio.btc,
            )
        return PipelinePlan(decision, approval)
