"""FastAPI composition root with safe observability and versioned routes."""

import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from tradingagent.api.models import (
    AcceptedOperation,
    DatasetList,
    ErrorDetail,
    ErrorResponse,
    HealthView,
    JobView,
    OperationRequest,
    ResourceView,
    StrategyValidationRequest,
)
from tradingagent.api.services import ApplicationError, ApplicationRegistry
from tradingagent.config import AppConfig

LOGGER = logging.getLogger("tradingagent.api")
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def _correlation_id(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", uuid.uuid4()))


def _error(request: Request, code: str, message: str, status: int) -> JSONResponse:
    payload = ErrorResponse(
        error=ErrorDetail(code=code, message=message, correlation_id=_correlation_id(request))
    )
    return JSONResponse(status_code=status, content=payload.model_dump(exclude_none=True))


def create_app(registry: ApplicationRegistry | None = None) -> FastAPI:
    """Build an isolated application instance for a process or test."""
    services = registry or ApplicationRegistry()
    metrics_registry = CollectorRegistry()
    requests = Counter(
        "tradingagent_http_requests_total",
        "HTTP requests by method and status",
        ("method", "status"),
        registry=metrics_registry,
    )
    latency = Histogram(
        "tradingagent_http_request_duration_seconds",
        "HTTP request latency",
        ("method",),
        registry=metrics_registry,
    )
    jobs = Counter(
        "tradingagent_jobs_total",
        "Jobs accepted by kind",
        ("kind",),
        registry=metrics_registry,
    )
    app = FastAPI(
        title="tradingagent internal API",
        version="1.0.0",
        description="Internal-only backtest and paper-trading control plane. No live orders.",
    )
    app.state.registry = services

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[PlainTextResponse]],
    ) -> PlainTextResponse:
        correlation = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request.state.correlation_id = correlation[:128]
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        requests.labels(request.method, str(response.status_code)).inc()
        latency.labels(request.method).observe(time.monotonic() - started)
        LOGGER.info(
            "request_completed",
            extra={
                "event": "request_completed",
                "correlation_id": request.state.correlation_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
            },
        )
        return response

    @app.exception_handler(ApplicationError)
    async def application_error(request: Request, exc: ApplicationError) -> JSONResponse:
        return _error(request, exc.code, exc.message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details: dict[str, object] = {
            "fields": [".".join(str(part) for part in item["loc"]) for item in exc.errors()]
        }
        payload = ErrorResponse(
            error=ErrorDetail(
                code="validation_error",
                message="Request validation failed",
                correlation_id=_correlation_id(request),
                details=details,
            )
        )
        return JSONResponse(status_code=422, content=payload.model_dump(exclude_none=True))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        del exc
        LOGGER.error(
            "unexpected_error",
            extra={
                "event": "unexpected_error",
                "correlation_id": _correlation_id(request),
            },
        )
        return _error(request, "internal_error", "An internal error occurred", 500)

    def require_idempotency(value: str | None) -> str:
        if value is None or not value.strip() or len(value) > 128:
            raise ApplicationError(
                "missing_idempotency_key",
                "A non-empty Idempotency-Key header is required",
                400,
            )
        return value

    @app.get("/health/live", response_model=HealthView, response_model_exclude_none=True)
    def live() -> HealthView:
        return HealthView(status="ok")

    @app.get("/health/ready", response_model=HealthView)
    def ready() -> JSONResponse:
        status, dependencies = services.readiness()
        body = HealthView(status=status, dependencies=dependencies)
        return JSONResponse(
            status_code=503 if status == "not_ready" else 200, content=body.model_dump()
        )

    @app.get("/metrics", include_in_schema=True)
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            generate_latest(metrics_registry).decode(),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.get("/api/v1/datasets", response_model=DatasetList)
    def datasets() -> DatasetList:
        return DatasetList(datasets=services.datasets)

    @app.post(
        "/api/v1/data/sync", response_model=JobView, status_code=202, responses=ERROR_RESPONSES
    )
    def sync(
        payload: OperationRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> object:
        key = require_idempotency(idempotency_key)
        result = services.idempotent(
            key,
            "data_sync",
            payload.model_dump(),
            lambda: services.create_job("data_sync", payload.model_dump(), idempotency_key=key),
        )
        jobs.labels("data_sync").inc()
        return result

    @app.get("/api/v1/data/gaps")
    def gaps(dataset_id: str) -> dict[str, object]:
        return {"dataset_id": dataset_id, "gaps": services.gaps(dataset_id)}

    @app.post("/api/v1/strategies/validate", responses=ERROR_RESPONSES)
    def validate_strategy(
        payload: StrategyValidationRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> object:
        key = require_idempotency(idempotency_key)

        def validate() -> dict[str, object]:
            try:
                configuration = AppConfig.model_validate(payload.configuration)
            except ValueError as exc:
                raise ApplicationError(
                    "invalid_configuration", "Strategy configuration is invalid", 422
                ) from exc
            return {
                "valid": True,
                "configuration": configuration.model_dump(mode="json"),
            }

        return services.idempotent(
            key,
            "strategy_validate",
            payload.model_dump(),
            validate,
        )

    def create_resource(kind: str, payload: OperationRequest, key: str | None) -> object:
        idempotency_key = require_idempotency(key)
        result = services.idempotent(
            idempotency_key,
            f"{kind}_create",
            payload.model_dump(),
            lambda: services.create(kind, payload.model_dump()),
        )
        jobs.labels(f"{kind}_create").inc()
        return result

    @app.post(
        "/api/v1/backtests",
        response_model=AcceptedOperation,
        status_code=202,
        responses=ERROR_RESPONSES,
    )
    def create_backtest(
        payload: OperationRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> object:
        return create_resource("backtest", payload, idempotency_key)

    @app.get(
        "/api/v1/backtests/{resource_id}", response_model=ResourceView, responses=ERROR_RESPONSES
    )
    def backtest(resource_id: str) -> ResourceView:
        return services.resource(resource_id, "backtest")

    @app.get("/api/v1/backtests/{resource_id}/report", responses=ERROR_RESPONSES)
    def backtest_report(resource_id: str) -> dict[str, object]:
        return services.backtest_report(resource_id)

    @app.post(
        "/api/v1/paper-sessions",
        response_model=AcceptedOperation,
        status_code=202,
        responses=ERROR_RESPONSES,
    )
    def create_paper(
        payload: OperationRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> object:
        return create_resource("paper", payload, idempotency_key)

    def transition(resource_id: str, action: str, key: str | None) -> object:
        idempotency_key = require_idempotency(key)
        return services.idempotent(
            idempotency_key,
            f"paper_{resource_id}_{action}",
            {},
            lambda: services.transition(resource_id, action),
        )

    for action in ("start", "pause", "resume", "stop"):

        def endpoint(
            resource_id: str,
            idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
            operation: str = action,
        ) -> object:
            return transition(resource_id, operation, idempotency_key)

        app.add_api_route(
            f"/api/v1/paper-sessions/{{resource_id}}/{action}",
            endpoint,
            methods=["POST"],
            response_model=ResourceView,
            responses=ERROR_RESPONSES,
            name=f"paper_{action}",
        )

    @app.get(
        "/api/v1/paper-sessions/{resource_id}",
        response_model=ResourceView,
        responses=ERROR_RESPONSES,
    )
    def paper(resource_id: str) -> ResourceView:
        return services.resource(resource_id, "paper_session")

    @app.get("/api/v1/paper-sessions/{resource_id}/decisions", responses=ERROR_RESPONSES)
    def decisions(resource_id: str) -> dict[str, object]:
        return {"session_id": resource_id, "decisions": services.decisions(resource_id)}

    @app.get("/api/v1/paper-sessions/{resource_id}/ledger", responses=ERROR_RESPONSES)
    def ledger(resource_id: str) -> dict[str, object]:
        return {"session_id": resource_id, "entries": services.ledger(resource_id)}

    @app.get("/api/v1/jobs/{resource_id}", response_model=JobView, responses=ERROR_RESPONSES)
    def job(resource_id: str) -> JobView:
        return services.job(resource_id)

    return app


def _runtime_registry() -> ApplicationRegistry:
    database_url = os.getenv("DATABASE_URL")
    return (
        ApplicationRegistry.from_database_url(database_url)
        if database_url
        else ApplicationRegistry()
    )


app = create_app(_runtime_registry())
