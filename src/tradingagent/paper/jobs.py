"""Idempotent and observable synchronous job reference implementation."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol, TypeVar

T = TypeVar("T")


class JobStatus(StrEnum):
    """Persisted background job states."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class JobRecord:
    """Progress and retry state exposed to API, CLI and monitoring."""

    job_type: str
    idempotency_key: str
    status: JobStatus
    completed_units: int
    total_units: int | None
    retry_count: int
    terminal_error: str | None
    result: object | None
    updated_at: datetime


class JobRepository(Protocol):
    """Persistence contract for idempotent background jobs."""

    def get(self, job_type: str, idempotency_key: str) -> JobRecord | None: ...
    def save(self, record: JobRecord) -> JobRecord: ...


@dataclass
class InMemoryJobRepository:
    """In-memory reference adapter with a composite unique key."""

    _records: dict[tuple[str, str], JobRecord] = field(default_factory=dict)

    def get(self, job_type: str, idempotency_key: str) -> JobRecord | None:
        """Look up a job by its stable operation key."""
        return self._records.get((job_type, idempotency_key))

    def save(self, record: JobRecord) -> JobRecord:
        """Persist the latest observable job state."""
        self._records[(record.job_type, record.idempotency_key)] = record
        return record


class JobRunner:
    """Run restart-safe jobs with persisted progress and bounded retries."""

    def __init__(self, repository: JobRepository, maximum_retries: int = 0) -> None:
        if maximum_retries < 0:
            raise ValueError("maximum_retries cannot be negative")
        self.repository = repository
        self.maximum_retries = maximum_retries

    def run(
        self,
        job_type: str,
        idempotency_key: str,
        work: Callable[[Callable[[int, int | None], None]], T],
        *,
        now: datetime,
    ) -> JobRecord:
        """Return a prior terminal result or execute with observable retries."""
        existing = self.repository.get(job_type, idempotency_key)
        if existing is not None and existing.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
            return existing
        record = existing or JobRecord(
            job_type, idempotency_key, JobStatus.RUNNING, 0, None, 0, None, None, now
        )
        self.repository.save(record)

        def progress(completed: int, total: int | None) -> None:
            nonlocal record
            if completed < 0 or (total is not None and (total < 0 or completed > total)):
                raise ValueError("invalid job progress")
            record = self.repository.save(
                replace(record, completed_units=completed, total_units=total, updated_at=now)
            )

        for attempt in range(self.maximum_retries + 1):
            try:
                result = work(progress)
            except Exception as exc:  # worker boundary records classified upstream errors
                if attempt < self.maximum_retries:
                    record = self.repository.save(
                        replace(record, retry_count=attempt + 1, updated_at=now)
                    )
                    continue
                return self.repository.save(
                    replace(
                        record,
                        status=JobStatus.FAILED,
                        retry_count=attempt,
                        terminal_error=f"{type(exc).__name__}: {exc}",
                        updated_at=now,
                    )
                )
            return self.repository.save(
                replace(
                    record,
                    status=JobStatus.SUCCEEDED,
                    result=result,
                    terminal_error=None,
                    updated_at=now,
                )
            )
        raise AssertionError("unreachable")
