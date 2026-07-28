"""Restart-safe paper application service using the shared trading engines."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256

from tradingagent.paper.models import (
    OrderStatus,
    PaperCheckpoint,
    PaperCycle,
    PaperOrder,
    PaperSessionState,
    ProcessedCandle,
    SessionAuditEvent,
    SessionStatus,
)
from tradingagent.paper.repository import PaperRepository
from tradingagent.trading.execution import ExecutionModel, OrderSide, SimulatedFill
from tradingagent.trading.ledger import AtomicLedger, LedgerInvariantError, LedgerSnapshot
from tradingagent.trading.risk import RiskEngine
from tradingagent.trading.strategy import DecisionAction, StrategyEngine


class PaperEngine:
    """Coordinate lifecycle, health gates, decisions, risk and atomic fills."""

    def __init__(
        self,
        *,
        repository: PaperRepository,
        strategy: StrategyEngine,
        risk: RiskEngine,
        execution: ExecutionModel,
        maximum_candle_age: timedelta,
    ) -> None:
        if maximum_candle_age <= timedelta(0):
            raise ValueError("maximum_candle_age must be positive")
        self.repository = repository
        self.strategy = strategy
        self.risk = risk
        self.execution = execution
        self.maximum_candle_age = maximum_candle_age

    def start(self, session_id: str, *, now: datetime) -> PaperSessionState:
        """Start a newly created session."""
        session = self.repository.get(session_id)
        if session.status in {SessionStatus.STOPPED, SessionStatus.FAILED}:
            raise ValueError("terminal session cannot be started")
        if session.status is not SessionStatus.CREATED:
            raise ValueError("only a created session can be started")
        return self._transition(session, SessionStatus.RUNNING, "start", "operator", "", now)

    def pause(
        self, session_id: str, *, actor: str, reason: str, now: datetime
    ) -> PaperSessionState:
        """Explicitly pause a running or degraded session."""
        session = self.repository.get(session_id)
        if session.status not in {SessionStatus.RUNNING, SessionStatus.DEGRADED}:
            raise ValueError("session cannot be paused from current state")
        return self._transition(session, SessionStatus.PAUSED, "pause", actor, reason, now)

    def resume(
        self,
        session_id: str,
        *,
        actor: str,
        now: datetime,
        clear_kill_switch: bool = False,
    ) -> PaperSessionState:
        """Resume explicitly; a risk latch requires separately asserted clearance."""
        session = self.repository.get(session_id)
        if session.status not in {SessionStatus.PAUSED, SessionStatus.DEGRADED}:
            raise ValueError("session cannot be resumed from current state")
        risk_state = session.risk_state
        if risk_state.paused:
            if not clear_kill_switch:
                raise ValueError("kill switch requires explicit risk resume")
            risk_state = self.risk.resume(risk_state, actor=actor)
            session = replace(session, risk_state=risk_state)
        return self._transition(session, SessionStatus.RUNNING, "resume", actor, "", now)

    def stop(self, session_id: str, *, actor: str, now: datetime) -> PaperSessionState:
        """Irreversibly stop a non-terminal session."""
        session = self.repository.get(session_id)
        if session.status in {SessionStatus.STOPPED, SessionStatus.FAILED}:
            raise ValueError("terminal session cannot be stopped again")
        return self._transition(session, SessionStatus.STOPPED, "stop", actor, "", now)

    def process(self, session_id: str, cycle: PaperCycle) -> PaperSessionState:
        """Process one closed candle exactly once, committing all effects atomically."""
        prior = self.repository.result_for_candle(session_id, cycle.candle_id)
        if prior is not None:
            return prior
        session = self.repository.get(session_id)
        if session.status is not SessionStatus.RUNNING:
            raise ValueError("paper cycle requires a running session")
        unhealthy = self._health_failure(session, cycle)
        if unhealthy is not None:
            return unhealthy
        if session.risk_state.paused:
            return self._transition(
                session,
                SessionStatus.PAUSED,
                "kill_switch_block",
                "risk-engine",
                session.risk_state.pause_reason or "risk kill switch",
                cycle.observed_at,
            )

        request = replace(
            cycle.strategy_request,
            has_position=session.ledger.portfolio.btc > 0,
        )
        decision = self.strategy.decide(request)
        order: PaperOrder | None = None
        fill: SimulatedFill | None = None
        updated = session
        if decision.action is DecisionAction.ENTER_LONG:
            approval = self.risk.approve_entry(
                portfolio=session.ledger.portfolio,
                state=session.risk_state,
                entry_price=cycle.reference_price,
                estimated_fee_rate=self.execution.config.taker_fee_rate,
                now=cycle.observed_at,
                atr=cycle.atr,
            )
            if approval.approved:
                order, fill, updated = self._execute(
                    session,
                    cycle,
                    decision.decision_id,
                    approval.risk_check_id,
                    OrderSide.BUY,
                    approval.quantity,
                )
                updated = replace(
                    updated,
                    risk_state=self.risk.record_entry(updated.risk_state, cycle.observed_at),
                )
        elif decision.action is DecisionAction.EXIT_LONG and session.ledger.portfolio.btc > 0:
            approval = self.risk.approve_exit(
                portfolio=session.ledger.portfolio,
                requested_quantity=session.ledger.portfolio.btc,
            )
            if approval.approved:
                order, fill, updated = self._execute(
                    session,
                    cycle,
                    decision.decision_id,
                    approval.risk_check_id,
                    OrderSide.SELL,
                    approval.quantity,
                )
                updated = replace(
                    updated,
                    risk_state=self.risk.record_exit(updated.risk_state, cycle.observed_at),
                )
        checkpoint = PaperCheckpoint(
            cycle.candle_id,
            cycle.candle_close_time,
            decision.decision_id,
            order.order_id if order else None,
            fill.fill_id if fill else None,
        )
        updated = replace(updated, checkpoint=checkpoint, last_error=None)
        return self.repository.commit_candle(ProcessedCandle(updated, decision, order, fill))

    def _execute(
        self,
        session: PaperSessionState,
        cycle: PaperCycle,
        decision_id: str,
        risk_check_id: str,
        side: OrderSide,
        quantity: Decimal,
    ) -> tuple[PaperOrder, SimulatedFill, PaperSessionState]:
        order_id = sha256(
            f"{session.session_id}:{cycle.candle_id}:{decision_id}:{side}".encode()
        ).hexdigest()
        fill_id = sha256(f"{order_id}:fill".encode()).hexdigest()
        fill = self.execution.fill(
            side=side,
            reference_price=cycle.reference_price,
            requested_quantity=quantity,
            atr=cycle.atr,
            fill_id=fill_id,
            decision_id=decision_id,
            risk_check_id=risk_check_id,
        )
        ledger = AtomicLedger(session.ledger.portfolio)
        try:
            delta = ledger.apply_fill(fill, occurred_at=cycle.observed_at)
        except LedgerInvariantError as exc:
            failed = replace(session, status=SessionStatus.FAILED, last_error=str(exc))
            self.repository.save(failed)
            raise
        combined = LedgerSnapshot(
            delta.portfolio,
            (*session.ledger.entries, *delta.entries),
            (*session.ledger.fills, *delta.fills),
        )
        order = PaperOrder(
            order_id,
            session.session_id,
            cycle.candle_id,
            decision_id,
            OrderStatus.FILLED,
            fill.fill_id,
        )
        return order, fill, replace(session, ledger=combined)

    def _health_failure(
        self, session: PaperSessionState, cycle: PaperCycle
    ) -> PaperSessionState | None:
        if not cycle.health.ledger_consistent:
            status, error = SessionStatus.FAILED, "ledger inconsistency"
        elif cycle.health.clock_drift:
            status, error = SessionStatus.PAUSED, "clock drift"
        elif not cycle.health.source_available:
            status, error = SessionStatus.DEGRADED, "source unavailable"
        elif cycle.health.has_gap:
            status, error = SessionStatus.DEGRADED, "data gap"
        elif cycle.observed_at - cycle.candle_close_time > self.maximum_candle_age:
            status, error = SessionStatus.DEGRADED, "stale candle"
        else:
            return None
        return self._transition(
            session, status, "fail_closed", "paper-engine", error, cycle.observed_at
        )

    def _transition(
        self,
        session: PaperSessionState,
        status: SessionStatus,
        event_type: str,
        actor: str,
        details: str,
        now: datetime,
    ) -> PaperSessionState:
        if not actor:
            raise ValueError("transition actor is required")
        event = SessionAuditEvent(event_type, now, actor, details)
        return self.repository.save(
            replace(
                session,
                status=status,
                last_error=details or None,
                audit_events=(*session.audit_events, event),
            )
        )
