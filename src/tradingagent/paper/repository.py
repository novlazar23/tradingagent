"""Persistence protocols and deterministic in-memory reference adapters."""

from dataclasses import dataclass, field
from typing import Protocol

from tradingagent.domain.models import Portfolio
from tradingagent.paper.models import (
    PaperOrder,
    PaperSessionState,
    ProcessedCandle,
    SessionStatus,
)
from tradingagent.trading.execution import SimulatedFill
from tradingagent.trading.ledger import AtomicLedger
from tradingagent.trading.risk import SessionRiskState
from tradingagent.trading.strategy import SignalDecision


class PaperRepository(Protocol):
    """Transaction boundary expected from a PostgreSQL paper repository."""

    def get(self, session_id: str) -> PaperSessionState: ...
    def save(self, session: PaperSessionState) -> PaperSessionState: ...
    def result_for_candle(self, session_id: str, candle_id: str) -> PaperSessionState | None: ...
    def commit_candle(self, result: ProcessedCandle) -> PaperSessionState: ...


@dataclass
class InMemoryPaperRepository:
    """In-memory adapter modeling unique and atomic database constraints."""

    _sessions: dict[str, PaperSessionState] = field(default_factory=dict)
    _decisions: dict[str, list[SignalDecision]] = field(default_factory=dict)
    _orders: dict[str, list[PaperOrder]] = field(default_factory=dict)
    _fills: dict[str, list[SimulatedFill]] = field(default_factory=dict)
    _processed: dict[tuple[str, str], PaperSessionState] = field(default_factory=dict)

    def create_session(
        self,
        *,
        session_id: str,
        portfolio: Portfolio,
        risk_state: SessionRiskState,
        configuration_versions: tuple[str, str, str],
    ) -> PaperSessionState:
        """Create a session with immutable configuration version references."""
        if session_id in self._sessions:
            raise ValueError("session_id already exists")
        if not session_id or any(not version for version in configuration_versions):
            raise ValueError("session and configuration versions are required")
        state = PaperSessionState(
            session_id=session_id,
            status=SessionStatus.CREATED,
            configuration_versions=configuration_versions,
            ledger=AtomicLedger(portfolio).snapshot(),
            risk_state=risk_state,
            checkpoint=None,
            last_error=None,
            audit_events=(),
        )
        self._sessions[session_id] = state
        return state

    def get(self, session_id: str) -> PaperSessionState:
        """Return the current state or raise for an unknown session."""
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise KeyError(f"unknown paper session: {session_id}") from exc

    def save(self, session: PaperSessionState) -> PaperSessionState:
        """Persist a non-candle state transition."""
        if session.session_id not in self._sessions:
            raise KeyError(f"unknown paper session: {session.session_id}")
        self._sessions[session.session_id] = session
        return session

    def result_for_candle(self, session_id: str, candle_id: str) -> PaperSessionState | None:
        """Return the prior atomic result for the idempotency key."""
        return self._processed.get((session_id, candle_id))

    def commit_candle(self, result: ProcessedCandle) -> PaperSessionState:
        """Atomically commit decision, optional order/fill, ledger and checkpoint."""
        if result.session.checkpoint is None:
            raise ValueError("committed candle requires a checkpoint")
        key = (result.session.session_id, result.session.checkpoint.candle_id)
        prior = self._processed.get(key)
        if prior is not None:
            return prior
        session_id = result.session.session_id
        self._decisions.setdefault(session_id, []).append(result.decision)
        if result.order is not None:
            self._orders.setdefault(session_id, []).append(result.order)
        if result.fill is not None:
            self._fills.setdefault(session_id, []).append(result.fill)
        self._sessions[session_id] = result.session
        self._processed[key] = result.session
        return result.session

    def decisions(self, session_id: str) -> tuple[SignalDecision, ...]:
        """Return immutable decision history."""
        return tuple(self._decisions.get(session_id, ()))

    def orders(self, session_id: str) -> tuple[PaperOrder, ...]:
        """Return immutable order history."""
        return tuple(self._orders.get(session_id, ()))

    def fills(self, session_id: str) -> tuple[SimulatedFill, ...]:
        """Return immutable fill history."""
        return tuple(self._fills.get(session_id, ()))
