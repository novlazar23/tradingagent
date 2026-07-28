"""Durable worker and scheduler loops over the shared jobs repository."""

import hashlib
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from tradingagent.api.services import ApplicationRegistry
from tradingagent.persistence.models import JobRecord, MarketDataset, PaperSession

Progress = Callable[[int], None]
Handler = Callable[[dict[str, object], Progress], dict[str, object] | None]


class RetryableJobError(RuntimeError):
    """Transient handler failure safe to retry after durable backoff."""


class DurableJobWorker:
    """Claim and execute one queued job with durable progress and bounded retries."""

    def __init__(
        self,
        registry: ApplicationRegistry,
        handlers: Mapping[str, Handler],
        *,
        maximum_retries: int = 3,
        worker_id: str | None = None,
        lease_duration: timedelta = timedelta(minutes=5),
        retry_jitter: Callable[[str, int], float] | None = None,
    ) -> None:
        self.registry = registry
        self.handlers = handlers
        self.maximum_retries = maximum_retries
        self.worker_id = worker_id or str(uuid.uuid4())
        self.lease_duration = lease_duration
        self.retry_jitter = retry_jitter or self._stable_jitter

    def run_once(self, *, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        with self.registry.sessions.begin() as session:
            statement = (
                select(JobRecord)
                .where(
                    JobRecord.available_at <= current,
                    or_(
                        JobRecord.status == "queued",
                        ((JobRecord.status == "running") & (JobRecord.lease_expires_at < current)),
                    ),
                )
                .order_by(JobRecord.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = session.scalar(statement)
            if job is None:
                return False
            job.status = "running"
            job.claimed_at = current
            job.lease_owner = self.worker_id
            job.lease_expires_at = current + self.lease_duration
            job.updated_at = current
            job_id = job.id
            kind = job.kind
            payload = job.payload

        handler = self.handlers.get(kind)
        if handler is None:
            self._fail(job_id, RuntimeError(f"no handler registered for {kind}"), current)
            return True

        def progress(value: int) -> None:
            if not 0 <= value <= 100:
                raise ValueError("progress must be between 0 and 100")
            with self.registry.sessions.begin() as session:
                record = session.get(JobRecord, job_id)
                if record is not None:
                    record.progress = value
                    record.updated_at = current

        try:
            for attempt in range(self.maximum_retries + 1):
                try:
                    result = handler(payload, progress)
                    break
                except ConnectionError:
                    if attempt >= self.maximum_retries:
                        raise
                    with self.registry.sessions.begin() as session:
                        record = session.get(JobRecord, job_id)
                        if record is not None:
                            record.retry_count += 1
                            record.updated_at = current
        except RetryableJobError as exc:
            with self.registry.sessions.begin() as session:
                record = session.get(JobRecord, job_id)
                if record is None:
                    return True
                if record.retry_count < min(record.maximum_retries, self.maximum_retries):
                    record.retry_count += 1
                    delay = 2**record.retry_count + self.retry_jitter(job_id, record.retry_count)
                    record.status = "queued"
                    record.available_at = current + timedelta(seconds=delay)
                    record.error_class = type(exc).__name__
                    record.error_message = "The operation will be retried"
                    record.lease_owner = None
                    record.lease_expires_at = None
                    record.updated_at = current
                    return True
            self._fail(job_id, exc, current)
            return True
        except Exception as exc:
            self._fail(job_id, exc, current)
            return True
        else:
            with self.registry.sessions.begin() as session:
                record = session.get(JobRecord, job_id)
                if record is not None:
                    record.status = "completed"
                    record.progress = 100
                    record.result = result
                    record.error_class = None
                    record.error_message = None
                    record.lease_owner = None
                    record.lease_expires_at = None
                    record.updated_at = current
            return True

    @staticmethod
    def _stable_jitter(job_id: str, attempt: int) -> float:
        digest = hashlib.sha256(f"{job_id}:{attempt}".encode()).digest()
        return int.from_bytes(digest[:2], "big") / 65535

    def _fail(self, job_id: str, error: Exception, now: datetime) -> None:
        with self.registry.sessions.begin() as session:
            record = session.get(JobRecord, job_id)
            if record is not None:
                record.status = "failed"
                record.error_class = type(error).__name__
                record.error_message = "The operation failed"
                record.lease_owner = None
                record.lease_expires_at = None
                record.updated_at = now


class JobScheduler:
    """Enqueue one idempotent paper cycle for each running due session."""

    def __init__(self, registry: ApplicationRegistry, *, interval: timedelta) -> None:
        if interval <= timedelta(0):
            raise ValueError("interval must be positive")
        self.registry = registry
        self.interval = interval

    def enqueue_due(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        due: list[str] = []
        with self.registry.sessions.begin() as session:
            rows = session.scalars(
                select(PaperSession).where(PaperSession.state == "running").with_for_update()
            ).all()
            for paper in rows:
                last = paper.last_scheduled_at
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                if last is None or last + self.interval <= current:
                    paper.last_scheduled_at = current
                    due.append(paper.id)
        bucket = int(current.timestamp() // self.interval.total_seconds())
        for session_id in due:
            self.registry.create_job(
                "paper_cycle",
                {"session_id": session_id, "scheduled_at": current.isoformat()},
                idempotency_key=f"{session_id}:{bucket}",
            )
        return len(due)

    def enqueue_data_syncs(self, now: datetime | None = None) -> int:
        """Enqueue one sync per selected dataset and scheduling interval."""
        current = now or datetime.now(UTC)
        with self.registry.sessions() as session:
            dataset_ids = list(
                session.scalars(
                    select(MarketDataset.id).where(MarketDataset.selected.is_(True))
                ).all()
            )
        bucket = int(current.timestamp() // self.interval.total_seconds())
        created = 0
        for dataset_id in dataset_ids:
            key = f"{dataset_id}:{bucket}"
            with self.registry.sessions() as session:
                exists = session.scalar(
                    select(JobRecord.id).where(
                        JobRecord.kind == "data_sync",
                        JobRecord.idempotency_key == key,
                    )
                )
            if exists is None:
                self.registry.create_job(
                    "data_sync",
                    {"dataset_id": dataset_id, "scheduled_at": current.isoformat()},
                    idempotency_key=key,
                )
                created += 1
        return created
