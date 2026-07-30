"""Persistence protocols and transactional PostgreSQL/in-memory adapters."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol, cast

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from tradingagent.domain.models import LedgerEntry, Portfolio
from tradingagent.paper.models import (
    PaperCheckpoint,
    PaperOrder,
    PaperSessionState,
    PendingOrderIntent,
    ProcessedCandle,
    SessionAuditEvent,
    SessionStatus,
)
from tradingagent.persistence.models import (
    AuditEventRecord,
    CashLedgerRecord,
    FillRecord,
    OrderRecord,
    PaperSession,
    PositionRecord,
    SignalDecisionRecord,
)
from tradingagent.trading.execution import OrderSide, SimulatedFill
from tradingagent.trading.ledger import AtomicLedger, LedgerSnapshot
from tradingagent.trading.risk import RiskAuditEvent, SessionRiskState
from tradingagent.trading.strategy import SignalDecision


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _state_payload(state: PaperSessionState) -> dict[str, object]:
    risk = state.risk_state
    return {
        "configuration_versions": list(state.configuration_versions),
        "risk": {
            "peak_equity": str(risk.peak_equity),
            "paused": risk.paused,
            "pause_reason": risk.pause_reason,
            "entries_by_day": [[day.isoformat(), count] for day, count in risk.entries_by_day],
            "daily_start_equity": [
                [day.isoformat(), str(value)] for day, value in risk.daily_start_equity
            ],
            "last_exit_at": risk.last_exit_at.isoformat() if risk.last_exit_at else None,
            "audit_events": [
                {
                    "event_type": event.event_type,
                    "occurred_at": event.occurred_at.isoformat(),
                    "details": event.details,
                }
                for event in risk.audit_events
            ],
        },
        "checkpoint": (
            {
                "candle_id": state.checkpoint.candle_id,
                "candle_close_time": state.checkpoint.candle_close_time.isoformat(),
                "decision_id": state.checkpoint.decision_id,
                "order_id": state.checkpoint.order_id,
                "fill_id": state.checkpoint.fill_id,
            }
            if state.checkpoint
            else None
        ),
        "last_error": state.last_error,
        "stop_price": str(state.stop_price) if state.stop_price is not None else None,
        "take_profit_price": (
            str(state.take_profit_price) if state.take_profit_price is not None else None
        ),
        "pending_intent": (
            {
                "decision_id": state.pending_intent.decision_id,
                "risk_check_id": state.pending_intent.risk_check_id,
                "action": state.pending_intent.action,
                "quantity": str(state.pending_intent.quantity),
                "decided_at": state.pending_intent.decided_at.isoformat(),
                "stop_price": (
                    str(state.pending_intent.stop_price)
                    if state.pending_intent.stop_price is not None
                    else None
                ),
                "take_profit_price": (
                    str(state.pending_intent.take_profit_price)
                    if state.pending_intent.take_profit_price is not None
                    else None
                ),
                "atr": (
                    str(state.pending_intent.atr) if state.pending_intent.atr is not None else None
                ),
            }
            if state.pending_intent
            else None
        ),
    }


def _risk(payload: dict[str, Any]) -> SessionRiskState:
    events = tuple(
        RiskAuditEvent(
            str(item["event_type"]),
            datetime.fromisoformat(str(item["occurred_at"])),
            str(item["details"]),
        )
        for item in payload.get("audit_events", [])
    )
    return SessionRiskState(
        Decimal(str(payload["peak_equity"])),
        bool(payload["paused"]),
        str(payload["pause_reason"]) if payload.get("pause_reason") else None,
        tuple(
            (date.fromisoformat(str(day)), int(count)) for day, count in payload["entries_by_day"]
        ),
        tuple(
            (date.fromisoformat(str(day)), Decimal(str(value)))
            for day, value in payload["daily_start_equity"]
        ),
        _dt(str(payload["last_exit_at"])) if payload.get("last_exit_at") else None,
        events,
    )


def _pending_intent(payload: object) -> PendingOrderIntent | None:
    if not isinstance(payload, dict):
        return None
    return PendingOrderIntent(
        decision_id=str(payload["decision_id"]),
        risk_check_id=str(payload["risk_check_id"]),
        action=str(payload["action"]),
        quantity=Decimal(str(payload["quantity"])),
        decided_at=datetime.fromisoformat(str(payload["decided_at"])),
        stop_price=(Decimal(str(payload["stop_price"])) if payload.get("stop_price") else None),
        take_profit_price=(
            Decimal(str(payload["take_profit_price"])) if payload.get("take_profit_price") else None
        ),
        atr=Decimal(str(payload["atr"])) if payload.get("atr") else None,
    )


class PaperRepository(Protocol):
    """Transaction boundary expected from a PostgreSQL paper repository."""

    def get(self, session_id: str) -> PaperSessionState: ...
    def save(self, session: PaperSessionState) -> PaperSessionState: ...
    def result_for_candle(self, session_id: str, candle_id: str) -> PaperSessionState | None: ...
    def commit_candle(self, result: ProcessedCandle) -> PaperSessionState: ...


class SQLAlchemyPaperRepository:
    """Map complete paper state to normalized records in one DB transaction."""

    def __init__(
        self,
        engine: Engine,
        *,
        failure_injector: Callable[[str], None] | None = None,
        transaction_guard: Callable[[Any], None] | None = None,
    ) -> None:
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.failure_injector = failure_injector or (lambda _stage: None)
        self.transaction_guard = transaction_guard or (lambda _session: None)

    def create_session(
        self,
        *,
        session_id: str,
        portfolio: Portfolio,
        risk_state: SessionRiskState,
        configuration_versions: tuple[str, str, str],
        request: dict[str, object] | None = None,
    ) -> PaperSessionState:
        if not session_id or any(not value for value in configuration_versions):
            raise ValueError("session and configuration versions are required")
        state = PaperSessionState(
            session_id,
            SessionStatus.CREATED,
            configuration_versions,
            AtomicLedger(portfolio).snapshot(),
            risk_state,
            None,
            None,
            (),
        )
        now = datetime.now(UTC)
        with self.sessions.begin() as db:
            self.transaction_guard(db)
            paper = db.get(PaperSession, session_id)
            if paper is not None and "_state" in paper.request:
                raise ValueError("session_id already exists")
            if paper is None:
                paper = PaperSession(
                    id=session_id,
                    state=state.status.value.lower(),
                    request={**(request or {}), "_state": _state_payload(state)},
                    created_at=now,
                    updated_at=now,
                )
                db.add(paper)
            else:
                preserved = dict(paper.request)
                preserved.update(request or {})
                preserved["_state"] = _state_payload(state)
                paper.request = preserved
                paper.state = state.status.value.lower()
                paper.updated_at = now
            db.add(
                PositionRecord(
                    session_id=session_id,
                    btc_quantity=portfolio.btc,
                    average_entry_price=None,
                    updated_at=now,
                )
            )
            if portfolio.cash:
                db.add(
                    CashLedgerRecord(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"paper:{session_id}:initial")),
                        session_id=session_id,
                        entry_type="deposit",
                        asset="USDT",
                        amount=portfolio.cash,
                        reference_id="initial-capital",
                        occurred_at=now,
                    )
                )
        return state

    def get(self, session_id: str) -> PaperSessionState:
        with self.sessions() as db:
            paper = db.get(PaperSession, session_id)
            if paper is None:
                raise KeyError(f"unknown paper session: {session_id}")
            runtime = cast(dict[str, Any], paper.request.get("_state", {}))
            risk = _risk(runtime["risk"])
            checkpoint_payload = runtime.get("checkpoint")
            checkpoint = (
                PaperCheckpoint(
                    str(checkpoint_payload["candle_id"]),
                    datetime.fromisoformat(str(checkpoint_payload["candle_close_time"])),
                    str(checkpoint_payload["decision_id"]),
                    str(checkpoint_payload["order_id"])
                    if checkpoint_payload.get("order_id")
                    else None,
                    str(checkpoint_payload["fill_id"])
                    if checkpoint_payload.get("fill_id")
                    else None,
                )
                if isinstance(checkpoint_payload, dict)
                else None
            )
            entries = tuple(
                LedgerEntry(
                    cast(Any, row.entry_type),
                    cast(Any, row.asset),
                    row.amount,
                    _aware(row.occurred_at),
                    row.reference_id,
                )
                for row in db.scalars(
                    select(CashLedgerRecord)
                    .where(
                        CashLedgerRecord.session_id == session_id,
                        CashLedgerRecord.reference_id != "initial-capital",
                    )
                    .order_by(CashLedgerRecord.occurred_at, CashLedgerRecord.id)
                )
            )
            order_rows = db.scalars(
                select(OrderRecord).where(OrderRecord.session_id == session_id)
            ).all()
            sides = {row.id: row.side for row in order_rows}
            fills = tuple(
                _fill(row, sides[row.order_id])
                for row in db.scalars(
                    select(FillRecord)
                    .join(OrderRecord, FillRecord.order_id == OrderRecord.id)
                    .where(OrderRecord.session_id == session_id)
                    .order_by(FillRecord.occurred_at)
                )
            )
            position = db.get(PositionRecord, session_id)
            initial = db.scalar(
                select(CashLedgerRecord).where(
                    CashLedgerRecord.session_id == session_id,
                    CashLedgerRecord.reference_id == "initial-capital",
                )
            )
            assert position is not None and initial is not None
            cash = initial.amount + sum(
                (row.amount for row in entries if row.asset == "USDT"), Decimal(0)
            )
            audit_events = tuple(
                SessionAuditEvent(
                    row.event_type,
                    _aware(row.occurred_at),
                    str(row.payload.get("actor", "system")),
                    str(row.payload.get("details", "")),
                )
                for row in db.scalars(
                    select(AuditEventRecord)
                    .where(AuditEventRecord.subject_id == session_id)
                    .order_by(AuditEventRecord.occurred_at)
                )
            )
            return PaperSessionState(
                session_id,
                SessionStatus(paper.state.upper()),
                tuple(runtime["configuration_versions"]),
                LedgerSnapshot(Portfolio(cash, position.btc_quantity), entries, fills),
                risk,
                checkpoint,
                str(runtime["last_error"]) if runtime.get("last_error") else None,
                audit_events,
                Decimal(str(runtime["stop_price"])) if runtime.get("stop_price") else None,
                Decimal(str(runtime["take_profit_price"]))
                if runtime.get("take_profit_price")
                else None,
                _pending_intent(runtime.get("pending_intent")),
            )

    def save(self, state: PaperSessionState) -> PaperSessionState:
        with self.sessions.begin() as db:
            self.transaction_guard(db)
            self._save_state(db, state)
        return state

    def result_for_candle(self, session_id: str, candle_id: str) -> PaperSessionState | None:
        with self.sessions() as db:
            found = db.scalar(
                select(SignalDecisionRecord.id).where(
                    SignalDecisionRecord.paper_session_id == session_id,
                    SignalDecisionRecord.candle_id == candle_id,
                )
            )
        return self.get(session_id) if found else None

    def commit_candle(self, result: ProcessedCandle) -> PaperSessionState:
        checkpoint = result.session.checkpoint
        if checkpoint is None:
            raise ValueError("committed candle requires a checkpoint")
        with self.sessions.begin() as db:
            self.transaction_guard(db)
            existing = db.scalar(
                select(SignalDecisionRecord.id).where(
                    SignalDecisionRecord.paper_session_id == result.session.session_id,
                    SignalDecisionRecord.candle_id == checkpoint.candle_id,
                )
            )
            if existing:
                return self.get(result.session.session_id)
            decision = result.decision
            db.add(
                SignalDecisionRecord(
                    id=decision.decision_id,
                    paper_session_id=result.session.session_id,
                    candle_id=checkpoint.candle_id,
                    action=decision.action.value,
                    score=decision.aggregate_score,
                    explanation=_decision_payload(decision),
                    decided_at=decision.decision_time,
                )
            )
            db.flush()
            self.failure_injector("after_decision")
            if result.order and result.fill:
                fill = result.fill
                db.add(
                    OrderRecord(
                        id=result.order.order_id,
                        session_id=result.session.session_id,
                        decision_id=decision.decision_id,
                        side=fill.side.value,
                        status=result.order.status.value,
                        quantity=fill.quantity,
                        created_at=checkpoint.candle_close_time,
                        idempotency_key=f"{result.session.session_id}:{checkpoint.candle_id}",
                    )
                )
                db.flush()
                self.failure_injector("after_order")
                db.add(
                    FillRecord(
                        id=fill.fill_id,
                        order_id=result.order.order_id,
                        price=fill.fill_price,
                        quantity=fill.quantity,
                        fee=fill.fee,
                        details=_fill_payload(fill),
                        occurred_at=checkpoint.candle_close_time,
                    )
                )
                db.flush()
                self.failure_injector("after_fill")
            prior_entries = {
                row.reference_id + ":" + row.entry_type + ":" + row.asset
                for row in db.scalars(
                    select(CashLedgerRecord).where(
                        CashLedgerRecord.session_id == result.session.session_id
                    )
                )
            }
            for index, entry in enumerate(result.session.ledger.entries):
                key = entry.reference_id + ":" + entry.entry_type + ":" + entry.asset
                if key not in prior_entries:
                    db.add(
                        CashLedgerRecord(
                            id=str(
                                uuid.uuid5(
                                    uuid.NAMESPACE_URL,
                                    (
                                        f"paper:{result.session.session_id}:ledger:"
                                        f"{entry.reference_id}:{entry.entry_type}:{entry.asset}:{index}"
                                    ),
                                )
                            ),
                            session_id=result.session.session_id,
                            entry_type=entry.entry_type,
                            asset=entry.asset,
                            amount=entry.amount,
                            reference_id=entry.reference_id,
                            occurred_at=entry.occurred_at,
                        )
                    )
            self._save_state(db, result.session)
        return result.session

    def _save_state(self, db: Any, state: PaperSessionState) -> None:
        paper = db.get(PaperSession, state.session_id)
        if paper is None:
            raise KeyError(f"unknown paper session: {state.session_id}")
        request = dict(paper.request)
        request["_state"] = _state_payload(state)
        paper.request = request
        paper.state = state.status.value.lower()
        paper.updated_at = datetime.now(UTC)
        position = db.get(PositionRecord, state.session_id)
        position.btc_quantity = state.ledger.portfolio.btc
        position.updated_at = paper.updated_at
        existing = db.scalars(
            select(AuditEventRecord).where(AuditEventRecord.subject_id == state.session_id)
        ).all()
        seen_ids = {row.id for row in existing}
        for event in state.audit_events:
            event_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    (
                        f"paper:{state.session_id}:audit:{event.event_type}:"
                        f"{event.occurred_at.isoformat()}:{event.actor}:{event.details}"
                    ),
                )
            )
            if event_id not in seen_ids:
                db.add(
                    AuditEventRecord(
                        id=event_id,
                        event_type=event.event_type,
                        subject_id=state.session_id,
                        payload={"actor": event.actor, "details": event.details},
                        occurred_at=event.occurred_at,
                    )
                )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _fill_payload(fill: SimulatedFill) -> dict[str, object]:
    return {
        "reference_price": str(fill.reference_price),
        "notional": str(fill.notional),
        "spread_cost": str(fill.spread_cost),
        "slippage_cost": str(fill.slippage_cost),
        "execution_model_version": fill.execution_model_version,
        "decision_id": fill.decision_id,
        "risk_check_id": fill.risk_check_id,
    }


def _fill(row: FillRecord, side: str) -> SimulatedFill:
    details = row.details
    return SimulatedFill(
        row.id,
        OrderSide(side),
        Decimal(str(details["reference_price"])),
        row.price,
        row.quantity,
        Decimal(str(details["notional"])),
        row.fee,
        Decimal(str(details["spread_cost"])),
        Decimal(str(details["slippage_cost"])),
        str(details["execution_model_version"]),
        str(details["decision_id"]) if details.get("decision_id") else None,
        str(details["risk_check_id"]) if details.get("risk_check_id") else None,
    )


def _decision_payload(decision: SignalDecision) -> dict[str, object]:
    return {
        "input_fingerprint": decision.input_fingerprint,
        "strategy_version": decision.strategy_version,
        "engine_version": decision.engine_version,
        "aggregate_confidence": str(decision.aggregate_confidence),
        "confirming_groups": list(decision.confirming_groups),
        "reasons": list(decision.reasons),
        "contributions": [
            {
                "feature_id": item.feature_id,
                "feature_version": item.feature_version,
                "candle_ids": list(item.candle_ids),
                "group": item.group,
                "timeframe": item.timeframe,
                "score": str(item.score),
                "confidence": str(item.confidence),
                "reason": item.reason,
            }
            for item in decision.contributions
        ],
    }


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
