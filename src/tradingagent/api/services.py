"""Repository-backed application services shared by API, CLI and background roles."""

import hashlib
import json
import threading
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, Protocol

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tradingagent.api.models import AcceptedOperation, JobView, ResourceView
from tradingagent.paper.models import SessionAuditEvent, SessionStatus
from tradingagent.paper.repository import SQLAlchemyPaperRepository
from tradingagent.persistence.models import (
    BacktestMetric,
    BacktestRun,
    Base,
    CashLedgerRecord,
    DataGap,
    IdempotencyRecord,
    JobRecord,
    MarketDataset,
    PaperSession,
    SignalDecisionRecord,
)
from tradingagent.trading.risk import RiskAuditEvent


class ApplicationError(Exception):
    """Safe error carrying a stable public code and HTTP status."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _encode_cursor(identifier: str) -> str:
    return urlsafe_b64encode(identifier.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> str | None:
    if cursor is None:
        return None
    try:
        return urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApplicationError("invalid_cursor", "Cursor is invalid", 422) from exc


class _HasId(Protocol):
    id: str


def _page_result[T: _HasId](rows: list[T], limit: int) -> tuple[list[T], str | None]:
    has_more = len(rows) > limit
    page = rows[:limit]
    cursor = _encode_cursor(page[-1].id) if has_more and page else None
    return page, cursor


class ApplicationRegistry:
    """Transactional repository and application-service facade.

    An injected engine is the supported production/test composition. The default
    isolated SQLite database preserves a safe local CLI and unit-test experience;
    Compose injects PostgreSQL through :meth:`from_database_url`.
    """

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        database_ready: bool = True,
        migrations_ready: bool = True,
        configuration_ready: bool = True,
        history_ready: bool = True,
    ) -> None:
        self.engine = engine or create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        if engine is None:
            Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.database_ready_override = database_ready
        self.migrations_ready_override = migrations_ready
        self.configuration_ready = configuration_ready
        self.history_ready = history_ready
        self._idempotency_lock = threading.RLock()

    @classmethod
    def from_database_url(
        cls, database_url: str, *, configuration_ready: bool = True, history_ready: bool = True
    ) -> "ApplicationRegistry":
        """Create a shared registry without silently creating production tables."""
        return cls(
            engine=create_engine(database_url, pool_pre_ping=True),
            configuration_ready=configuration_ready,
            history_ready=history_ready,
        )

    @property
    def datasets(self) -> list[dict[str, object]]:
        return self.dataset_page(cursor=None, limit=500)[0]

    def dataset_page(
        self, *, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, object]], str | None]:
        after = _decode_cursor(cursor)
        with self.sessions() as session:
            statement = select(MarketDataset).order_by(MarketDataset.id)
            if after is not None:
                statement = statement.where(MarketDataset.id > after)
            records, next_cursor = _page_result(
                list(session.scalars(statement.limit(limit + 1)).all()), limit
            )
            items: list[dict[str, object]] = [
                {
                    "id": record.id,
                    "source": record.source,
                    "external_id": record.external_id,
                    "symbol": record.symbol,
                    "selected": record.selected,
                    **record.metadata_json,
                }
                for record in records
            ]
            return items, next_cursor

    def readiness(
        self,
    ) -> tuple[Literal["ok", "degraded", "not_ready"], dict[str, str]]:
        database = False
        migrations = False
        try:
            tables = set(inspect(self.engine).get_table_names())
            database = bool(tables) and self.database_ready_override
            schema_complete = set(Base.metadata.tables).issubset(tables)
            if self.engine.dialect.name == "postgresql" and "alembic_version" in tables:
                with self.engine.connect() as connection:
                    revisions = set(
                        connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalars()
                    )
                expected = set(ScriptDirectory.from_config(Config("alembic.ini")).get_heads())
                schema_complete = schema_complete and revisions == expected
            elif self.engine.dialect.name == "postgresql":
                schema_complete = False
            migrations = schema_complete and self.migrations_ready_override
        except SQLAlchemyError:
            pass
        dependencies = {
            "database": "available" if database else "unavailable",
            "migrations": "current" if migrations else "pending",
            "configuration": "valid" if self.configuration_ready else "invalid",
            "history": "available" if self.history_ready else "unavailable",
        }
        required = database and migrations and self.configuration_ready
        status: Literal["ok", "degraded", "not_ready"]
        status = "not_ready" if not required else ("degraded" if not self.history_ready else "ok")
        return status, dependencies

    @staticmethod
    def _fingerprint(operation: str, payload: object) -> str:
        return hashlib.sha256(
            json.dumps(
                {"operation": operation, "payload": payload},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()

    @staticmethod
    def _restore(result_type: str, result: dict[str, object]) -> object:
        if result_type == "AcceptedOperation":
            return AcceptedOperation.model_validate(result)
        if result_type == "JobView":
            return JobView.model_validate(result)
        if result_type == "ResourceView":
            return ResourceView.model_validate(result)
        return result

    def idempotent(self, key: str, operation: str, payload: object, factory: object) -> object:
        fingerprint = self._fingerprint(operation, payload)
        if not callable(factory):
            raise TypeError("factory must be callable")
        with self._idempotency_lock:
            with self.sessions() as session:
                previous = session.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.operation == operation,
                        IdempotencyRecord.idempotency_key == key,
                    )
                )
                if previous is not None:
                    if previous.fingerprint != fingerprint:
                        raise ApplicationError(
                            "idempotency_conflict",
                            "The idempotency key was already used with a different request",
                            409,
                        )
                    return self._restore(previous.result_type, previous.result)
            result = factory()
            result_type = type(result).__name__
            serialized = (
                result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)
            )
            with self.sessions.begin() as session:
                session.add(
                    IdempotencyRecord(
                        id=str(uuid.uuid4()),
                        operation=operation,
                        idempotency_key=key,
                        fingerprint=fingerprint,
                        result_type=result_type,
                        result=serialized,
                        created_at=_utcnow(),
                    )
                )
                try:
                    session.flush()
                except IntegrityError as exc:
                    raise ApplicationError(
                        "idempotency_conflict", "Concurrent request conflict", 409
                    ) from exc
            return result

    def create(self, kind: str, payload: dict[str, object]) -> AcceptedOperation:
        resource_id, job_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = _utcnow()
        with self.sessions.begin() as session:
            if kind == "backtest":
                session.add(
                    BacktestRun(
                        id=resource_id,
                        state="queued",
                        request=payload,
                        result=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                session.add(
                    PaperSession(
                        id=resource_id,
                        state="created",
                        request=payload,
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.add(
                JobRecord(
                    id=job_id,
                    kind=f"{kind}_create",
                    status="queued",
                    progress=0,
                    retry_count=0,
                    maximum_retries=3,
                    payload={"resource_id": resource_id, **payload},
                    idempotency_key=f"resource:{resource_id}",
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        base = "/api/v1/backtests" if kind == "backtest" else "/api/v1/paper-sessions"
        return AcceptedOperation(
            resource_id=resource_id,
            resource_url=f"{base}/{resource_id}",
            job_id=job_id,
            job_url=f"/api/v1/jobs/{job_id}",
        )

    def create_job(
        self,
        kind: str,
        payload: dict[str, object] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> JobView:
        now, job_id = _utcnow(), str(uuid.uuid4())
        operation_key = idempotency_key or job_id
        with self.sessions.begin() as session:
            existing = session.scalar(
                select(JobRecord).where(
                    JobRecord.kind == kind,
                    JobRecord.idempotency_key == operation_key,
                )
            )
            if existing is not None:
                return self._job_view(existing)
            record = JobRecord(
                id=job_id,
                kind=kind,
                status="queued",
                progress=0,
                retry_count=0,
                maximum_retries=3,
                payload=payload or {},
                idempotency_key=operation_key,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            session.flush()
        return self._job_view(record)

    @staticmethod
    def _job_view(record: JobRecord) -> JobView:
        public_error_classes = {"data_error", "configuration_error", "dependency_error"}
        error_class = (
            record.error_class if record.error_class in public_error_classes else "internal_error"
        )
        return JobView(
            id=record.id,
            kind=record.kind,
            status=record.status,  # type: ignore[arg-type]
            progress=record.progress,
            retry_count=record.retry_count,
            error_class=error_class if record.error_class else None,
            error_message="Job failed" if record.error_message else None,
        )

    def resource(self, resource_id: str, expected_kind: str) -> ResourceView:
        with self.sessions() as session:
            if expected_kind == "backtest":
                backtest = session.get(BacktestRun, resource_id)
                if backtest is None:
                    raise ApplicationError("resource_not_found", "Resource not found", 404)
                return ResourceView(
                    id=backtest.id,
                    kind="backtest",
                    state=backtest.state,
                    request=backtest.request,
                )
            paper = session.get(PaperSession, resource_id)
            if paper is None:
                raise ApplicationError("resource_not_found", "Resource not found", 404)
            return ResourceView(
                id=paper.id,
                kind="paper_session",
                state=paper.state,
                request=paper.request,
            )

    def job(self, job_id: str) -> JobView:
        with self.sessions() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                raise ApplicationError("job_not_found", "Job not found", 404)
            return self._job_view(record)

    def transition(self, resource_id: str, action: str) -> ResourceView:
        transitions = {
            ("created", "start"): "running",
            ("paused", "resume"): "running",
            ("running", "pause"): "paused",
            ("created", "stop"): "stopped",
            ("running", "stop"): "stopped",
            ("paused", "stop"): "stopped",
        }
        with self.sessions() as session:
            persisted = session.get(PaperSession, resource_id)
            has_runtime_state = persisted is not None and "_state" in persisted.request
        if has_runtime_state:
            repository = SQLAlchemyPaperRepository(self.engine)
            state = repository.get(resource_id)
            target = transitions.get((state.status.value.lower(), action))
            if target is None:
                raise ApplicationError(
                    "invalid_state_transition",
                    f"Cannot {action} a session in state {state.status.value.lower()}",
                    409,
                )
            now = _utcnow()
            risk_state = state.risk_state
            if action == "resume" and risk_state.paused:
                risk_state = replace(
                    risk_state,
                    paused=False,
                    pause_reason=None,
                    audit_events=(
                        *risk_state.audit_events,
                        RiskAuditEvent("risk_resume", now, "api-admin"),
                    ),
                )
            updated = replace(
                state,
                status=SessionStatus(target.upper()),
                risk_state=risk_state,
                audit_events=(
                    *state.audit_events,
                    SessionAuditEvent(action, now, "api-admin", ""),
                ),
            )
            repository.save(updated)
            return ResourceView(
                id=resource_id,
                kind="paper_session",
                state=target,
                request=self.resource(resource_id, "paper_session").request,
            )
        with self.sessions.begin() as session:
            resource = session.get(PaperSession, resource_id)
            if resource is None:
                raise ApplicationError("resource_not_found", "Resource not found", 404)
            new_state = transitions.get((resource.state, action))
            if new_state is None:
                raise ApplicationError(
                    "invalid_state_transition",
                    f"Cannot {action} a session in state {resource.state}",
                    409,
                )
            resource.state, resource.updated_at = new_state, _utcnow()
            session.flush()
            return ResourceView(
                id=resource.id,
                kind="paper_session",
                state=resource.state,
                request=resource.request,
            )

    def gaps(self, dataset_id: str) -> list[dict[str, object]]:
        return self.gap_page(dataset_id, cursor=None, limit=500)[0]

    def gap_page(
        self, dataset_id: str, *, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, object]], str | None]:
        after = _decode_cursor(cursor)
        with self.sessions() as session:
            statement = select(DataGap).where(DataGap.dataset_id == dataset_id).order_by(DataGap.id)
            if after is not None:
                statement = statement.where(DataGap.id > after)
            rows, next_cursor = _page_result(
                list(session.scalars(statement.limit(limit + 1)).all()), limit
            )
            items: list[dict[str, object]] = [
                {
                    "id": row.id,
                    "timeframe": row.timeframe,
                    "start_time": row.start_time,
                    "end_time": row.end_time,
                    "reason": row.reason,
                    "resolved_at": row.resolved_at,
                }
                for row in rows
            ]
            return items, next_cursor

    def backtest_report(self, resource_id: str) -> dict[str, object]:
        with self.sessions() as session:
            run = session.get(BacktestRun, resource_id)
            if run is None:
                raise ApplicationError("resource_not_found", "Resource not found", 404)
            metrics = session.scalars(
                select(BacktestMetric).where(BacktestMetric.run_id == resource_id)
            ).all()
            return {
                "backtest_id": run.id,
                "status": run.state,
                "result": run.result,
                "metrics": {
                    row.name: str(row.value) if row.value is not None else row.payload
                    for row in metrics
                },
            }

    def decisions(self, session_id: str) -> list[dict[str, object]]:
        return self.decision_page(session_id, cursor=None, limit=500)[0]

    def decision_page(
        self, session_id: str, *, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, object]], str | None]:
        self.resource(session_id, "paper_session")
        after = _decode_cursor(cursor)
        with self.sessions() as session:
            statement = (
                select(SignalDecisionRecord)
                .where(SignalDecisionRecord.paper_session_id == session_id)
                .order_by(SignalDecisionRecord.id)
            )
            if after is not None:
                statement = statement.where(SignalDecisionRecord.id > after)
            rows, next_cursor = _page_result(
                list(session.scalars(statement.limit(limit + 1)).all()), limit
            )
            items: list[dict[str, object]] = [
                {
                    "id": row.id,
                    "action": row.action,
                    "score": str(row.score),
                    "explanation": row.explanation,
                    "decided_at": row.decided_at,
                }
                for row in rows
            ]
            return items, next_cursor

    def ledger(self, session_id: str) -> list[dict[str, object]]:
        return self.ledger_page(session_id, cursor=None, limit=500)[0]

    def ledger_page(
        self, session_id: str, *, cursor: str | None, limit: int
    ) -> tuple[list[dict[str, object]], str | None]:
        self.resource(session_id, "paper_session")
        after = _decode_cursor(cursor)
        with self.sessions() as session:
            statement = (
                select(CashLedgerRecord)
                .where(CashLedgerRecord.session_id == session_id)
                .order_by(CashLedgerRecord.id)
            )
            if after is not None:
                statement = statement.where(CashLedgerRecord.id > after)
            rows, next_cursor = _page_result(
                list(session.scalars(statement.limit(limit + 1)).all()), limit
            )
            items: list[dict[str, object]] = [
                {
                    "id": row.id,
                    "entry_type": row.entry_type,
                    "asset": row.asset,
                    "amount": str(row.amount),
                    "reference_id": row.reference_id,
                    "occurred_at": row.occurred_at,
                }
                for row in rows
            ]
            return items, next_cursor
