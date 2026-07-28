"""In-memory application ports preserving API contracts until repositories are wired."""

import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from typing import Literal

from tradingagent.api.models import AcceptedOperation, JobView, ResourceView


class ApplicationError(Exception):
    """Safe error carrying a stable public code and HTTP status."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class Replay:
    fingerprint: str
    result: object


class ApplicationRegistry:
    """Thread-safe reference adapter for jobs and run state.

    It intentionally performs no trading and exposes no exchange-order capability.
    """

    def __init__(
        self,
        *,
        database_ready: bool = True,
        migrations_ready: bool = True,
        configuration_ready: bool = True,
        history_ready: bool = True,
    ) -> None:
        self.database_ready = database_ready
        self.migrations_ready = migrations_ready
        self.configuration_ready = configuration_ready
        self.history_ready = history_ready
        self.datasets: list[dict[str, object]] = []
        self.jobs: dict[str, JobView] = {}
        self.resources: dict[str, ResourceView] = {}
        self.replays: dict[str, Replay] = {}
        self._lock = threading.RLock()

    def readiness(
        self,
    ) -> tuple[Literal["ok", "degraded", "not_ready"], dict[str, str]]:
        dependencies = {
            "database": "available" if self.database_ready else "unavailable",
            "migrations": "current" if self.migrations_ready else "pending",
            "configuration": "valid" if self.configuration_ready else "invalid",
            "history": "available" if self.history_ready else "unavailable",
        }
        required = self.database_ready and self.migrations_ready and self.configuration_ready
        return (
            ("degraded" if required and not self.history_ready else "ok", dependencies)
            if required
            else (
                "not_ready",
                dependencies,
            )
        )

    def idempotent(self, key: str, operation: str, payload: object, factory: object) -> object:
        fingerprint = hashlib.sha256(
            json.dumps(
                {"operation": operation, "payload": payload},
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        replay_key = f"{operation}:{key}"
        with self._lock:
            previous = self.replays.get(replay_key)
            if previous:
                if previous.fingerprint != fingerprint:
                    raise ApplicationError(
                        "idempotency_conflict",
                        "The idempotency key was already used with a different request",
                        409,
                    )
                return previous.result
            if not callable(factory):
                raise TypeError("factory must be callable")
            result = factory()
            self.replays[replay_key] = Replay(fingerprint, result)
            return result

    def create(self, kind: str, payload: dict[str, object]) -> AcceptedOperation:
        resource_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        resource_kind: Literal["backtest", "paper_session"] = (
            "backtest" if kind == "backtest" else "paper_session"
        )
        self.resources[resource_id] = ResourceView(
            id=resource_id,
            kind=resource_kind,
            state="queued" if kind == "backtest" else "created",
            request=payload,
        )
        self.jobs[job_id] = JobView(
            id=job_id,
            kind=f"{kind}_create",
            status="queued",
            progress=0,
            retry_count=0,
        )
        base = "/api/v1/backtests" if kind == "backtest" else "/api/v1/paper-sessions"
        return AcceptedOperation(
            resource_id=resource_id,
            resource_url=f"{base}/{resource_id}",
            job_id=job_id,
            job_url=f"/api/v1/jobs/{job_id}",
        )

    def create_job(self, kind: str) -> JobView:
        job_id = str(uuid.uuid4())
        job = JobView(
            id=job_id,
            kind=kind,
            status="queued",
            progress=0,
            retry_count=0,
        )
        self.jobs[job_id] = job
        return job

    def resource(self, resource_id: str, expected_kind: str) -> ResourceView:
        resource = self.resources.get(resource_id)
        if resource is None or resource.kind != expected_kind:
            raise ApplicationError("resource_not_found", "Resource not found", 404)
        return resource

    def job(self, job_id: str) -> JobView:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise ApplicationError("job_not_found", "Job not found", 404) from exc

    def transition(self, resource_id: str, action: str) -> ResourceView:
        resource = self.resource(resource_id, "paper_session")
        transitions = {
            ("created", "start"): "running",
            ("paused", "resume"): "running",
            ("running", "pause"): "paused",
            ("created", "stop"): "stopped",
            ("running", "stop"): "stopped",
            ("paused", "stop"): "stopped",
        }
        new_state = transitions.get((resource.state, action))
        if new_state is None:
            raise ApplicationError(
                "invalid_state_transition",
                f"Cannot {action} a session in state {resource.state}",
                409,
            )
        updated = resource.model_copy(update={"state": new_state})
        self.resources[resource_id] = updated
        return updated
