"""Persistable domain records for restart-safe paper trading."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from tradingagent.trading.execution import SimulatedFill
from tradingagent.trading.ledger import LedgerSnapshot
from tradingagent.trading.risk import SessionRiskState
from tradingagent.trading.strategy import SignalDecision, StrategyRequest


class SessionStatus(StrEnum):
    """Lifecycle states persisted for a paper session."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    DEGRADED = "DEGRADED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class OrderStatus(StrEnum):
    """Lifecycle states for simulated orders."""

    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class PaperOrder:
    """Persistable simulated order linked to its originating decision."""

    order_id: str
    session_id: str
    candle_id: str
    decision_id: str
    status: OrderStatus
    fill_id: str | None


@dataclass(frozen=True, slots=True)
class CandleHealth:
    """Pre-trade health observations supplied by the data boundary."""

    source_available: bool
    has_gap: bool
    clock_drift: bool
    ledger_consistent: bool


@dataclass(frozen=True, slots=True)
class PaperCycle:
    """Complete immutable input for one newly closed 15-minute candle."""

    candle_id: str
    candle_close_time: datetime
    observed_at: datetime
    reference_price: Decimal
    atr: Decimal | None
    strategy_request: StrategyRequest
    health: CandleHealth

    def __post_init__(self) -> None:
        for value in (self.candle_close_time, self.observed_at):
            if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
                raise ValueError("paper cycle timestamps must be timezone-aware UTC")
        if not self.candle_id or self.reference_price <= 0:
            raise ValueError("candle_id and positive reference_price are required")


@dataclass(frozen=True, slots=True)
class PaperCheckpoint:
    """Last fully committed candle boundary used for recovery."""

    candle_id: str
    candle_close_time: datetime
    decision_id: str
    order_id: str | None
    fill_id: str | None


@dataclass(frozen=True, slots=True)
class SessionAuditEvent:
    """Operator or engine state transition retained for observability."""

    event_type: str
    occurred_at: datetime
    actor: str
    details: str


@dataclass(frozen=True, slots=True)
class PaperSessionState:
    """Complete state needed to reconstruct a paper session after restart."""

    session_id: str
    status: SessionStatus
    configuration_versions: tuple[str, str, str]
    ledger: LedgerSnapshot
    risk_state: SessionRiskState
    checkpoint: PaperCheckpoint | None
    last_error: str | None
    audit_events: tuple[SessionAuditEvent, ...]


@dataclass(frozen=True, slots=True)
class ProcessedCandle:
    """Atomic repository record for a committed candle result."""

    session: PaperSessionState
    decision: SignalDecision
    order: PaperOrder | None
    fill: SimulatedFill | None
