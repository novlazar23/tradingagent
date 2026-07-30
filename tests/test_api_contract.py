import logging

import pytest
from fastapi.testclient import TestClient
from test_config import valid_config

from tradingagent.api.app import create_app
from tradingagent.api.services import ApplicationRegistry
from tradingagent.persistence.models import JobRecord


def client(*, database_ready: bool = True, history_ready: bool = True) -> TestClient:
    return TestClient(
        create_app(
            ApplicationRegistry(
                database_ready=database_ready,
                migrations_ready=database_ready,
                configuration_ready=True,
                history_ready=history_ready,
            )
        )
    )


def test_health_distinguishes_liveness_readiness_and_degraded_history() -> None:
    healthy = client()
    assert healthy.get("/health/live").json() == {"status": "ok"}
    assert healthy.get("/health/ready").status_code == 200

    degraded = client(history_ready=False).get("/health/ready")
    assert degraded.status_code == 200
    assert degraded.json()["status"] == "degraded"
    assert degraded.json()["dependencies"]["history"] == "unavailable"

    unavailable = client(database_ready=False).get("/health/ready")
    assert unavailable.status_code == 503
    assert unavailable.json()["status"] == "not_ready"


def test_openapi_contains_every_required_route_and_error_schema() -> None:
    schema = client().get("/openapi.json").json()
    expected = {
        "/health/live",
        "/health/ready",
        "/metrics",
        "/api/v1/datasets",
        "/api/v1/data/sync",
        "/api/v1/data/gaps",
        "/api/v1/strategies/validate",
        "/api/v1/backtests",
        "/api/v1/backtests/{resource_id}",
        "/api/v1/backtests/{resource_id}/report",
        "/api/v1/paper-sessions",
        "/api/v1/paper-sessions/{resource_id}/start",
        "/api/v1/paper-sessions/{resource_id}/pause",
        "/api/v1/paper-sessions/{resource_id}/resume",
        "/api/v1/paper-sessions/{resource_id}/stop",
        "/api/v1/paper-sessions/{resource_id}",
        "/api/v1/paper-sessions/{resource_id}/decisions",
        "/api/v1/paper-sessions/{resource_id}/ledger",
        "/api/v1/jobs/{resource_id}",
    }
    assert expected <= set(schema["paths"])
    assert "ErrorResponse" in schema["components"]["schemas"]


def test_mutation_requires_idempotency_key_and_replays_same_result() -> None:
    api = client()
    missing = api.post("/api/v1/data/sync", json={"dataset_id": "dataset-1"})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "missing_idempotency_key"
    assert missing.json()["error"]["correlation_id"]

    headers = {"Idempotency-Key": "sync-once"}
    first = api.post("/api/v1/data/sync", headers=headers, json={"dataset_id": "dataset-1"})
    second = api.post("/api/v1/data/sync", headers=headers, json={"dataset_id": "dataset-1"})
    assert first.status_code == 202
    assert second.json() == first.json()

    conflict = api.post(
        "/api/v1/data/sync",
        headers=headers,
        json={"dataset_id": "different"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_job_and_resource_lifecycle_contracts() -> None:
    api = client()
    created = api.post(
        "/api/v1/backtests",
        headers={"Idempotency-Key": "backtest-1"},
        json={"configuration_version": "strategy-v1", "dataset_id": "dataset-1"},
    )
    assert created.status_code == 202
    job = api.get(created.json()["job_url"]).json()
    assert job["status"] == "queued"
    assert job["progress"] == 0
    assert job["retry_count"] == 0

    backtest = api.get(created.json()["resource_url"])
    assert backtest.status_code == 200
    assert backtest.json()["kind"] == "backtest"

    paper = api.post(
        "/api/v1/paper-sessions",
        headers={"Idempotency-Key": "paper-1"},
        json={"configuration_version": "strategy-v1", "dataset_id": "dataset-1"},
    ).json()
    session_url = paper["resource_url"]
    assert (
        api.post(f"{session_url}/start", headers={"Idempotency-Key": "paper-start"}).json()["state"]
        == "running"
    )
    assert (
        api.post(f"{session_url}/pause", headers={"Idempotency-Key": "paper-pause"}).json()["state"]
        == "paused"
    )
    assert (
        api.post(f"{session_url}/resume", headers={"Idempotency-Key": "paper-resume"}).json()[
            "state"
        ]
        == "running"
    )
    assert (
        api.post(f"{session_url}/stop", headers={"Idempotency-Key": "paper-stop"}).json()["state"]
        == "stopped"
    )


def test_error_response_is_safe_and_correlation_id_is_propagated() -> None:
    api = client()
    response = api.get(
        "/api/v1/jobs/not-a-real-job",
        headers={
            "X-Correlation-ID": "request-123",
            "X-API-Key": "must-never-leak",
        },
    )
    assert response.status_code == 404
    assert response.headers["X-Correlation-ID"] == "request-123"
    payload = response.json()
    assert payload["error"]["correlation_id"] == "request-123"
    assert "must-never-leak" not in response.text


def test_prometheus_endpoint_exposes_http_and_job_metrics() -> None:
    api = client()
    api.get("/health/live")
    metrics = api.get("/metrics")
    assert metrics.status_code == 200
    assert "tradingagent_http_requests_total" in metrics.text
    assert "tradingagent_jobs_total" in metrics.text


@pytest.mark.parametrize(
    ("path", "code", "message"),
    [
        ("/api/v1/backtests/missing", "resource_not_found", "Resource not found"),
        ("/api/v1/backtests/missing/report", "resource_not_found", "Resource not found"),
        ("/api/v1/paper-sessions/missing", "resource_not_found", "Resource not found"),
        ("/api/v1/jobs/missing", "job_not_found", "Job not found"),
    ],
)
def test_read_routes_use_stable_safe_not_found_error_contract(
    path: str, code: str, message: str
) -> None:
    response = client().get(path, headers={"X-Correlation-ID": "golden-correlation"})

    assert response.status_code == 404
    assert response.json() == {
        "schema_version": "1",
        "error": {
            "code": code,
            "message": message,
            "correlation_id": "golden-correlation",
        },
    }


def test_request_log_record_is_structured_and_metrics_are_labelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="tradingagent.api")
    api = client()

    api.get("/health/live", headers={"X-Correlation-ID": "log-correlation"})
    metrics = api.get("/metrics").text

    record = next(item for item in caplog.records if item.msg == "request_completed")
    assert record.event == "request_completed"  # type: ignore[attr-defined]
    assert record.correlation_id == "log-correlation"  # type: ignore[attr-defined]
    assert record.method == "GET"  # type: ignore[attr-defined]
    assert record.path == "/health/live"  # type: ignore[attr-defined]
    assert record.status == 200  # type: ignore[attr-defined]
    assert 'method="GET",status="200"' in metrics


def test_strategy_validation_accepts_only_safe_runtime_sections_without_echo() -> None:
    api = client()
    configuration = valid_config()
    safe = {name: configuration[name] for name in ("strategy", "risk", "costs")}
    response = api.post(
        "/api/v1/strategies/validate",
        headers={"Idempotency-Key": "validate-safe"},
        json={"configuration": safe},
    )
    assert response.status_code == 200
    assert response.json() == {"valid": True}
    assert "database" not in response.text

    rejected = api.post(
        "/api/v1/strategies/validate",
        headers={"Idempotency-Key": "validate-unsafe"},
        json={"configuration": {**safe, "deployment": configuration["deployment"]}},
    )
    assert rejected.status_code == 422
    assert "database" not in rejected.text


def test_collection_endpoints_enforce_cursor_page_size_cap() -> None:
    api = client()
    for path in ("/api/v1/datasets", "/api/v1/data/gaps?dataset_id=dataset-1"):
        separator = "&" if "?" in path else "?"
        assert api.get(f"{path}{separator}limit=501").status_code == 422

    paper = api.post(
        "/api/v1/paper-sessions",
        headers={"Idempotency-Key": "pagination-paper"},
        json={"configuration_version": "strategy-v1", "dataset_id": "dataset-1"},
    ).json()
    for suffix in ("decisions", "ledger"):
        assert api.get(f"{paper['resource_url']}/{suffix}?limit=501").status_code == 422


def test_metrics_define_runtime_trading_and_failure_series() -> None:
    metrics = client().get("/metrics").text
    for name in (
        "tradingagent_job_duration_seconds",
        "tradingagent_job_failures_total",
        "tradingagent_job_retries_total",
        "tradingagent_candles_processed_total",
        "tradingagent_signal_decisions_total",
        "tradingagent_risk_rejections_total",
        "tradingagent_portfolio_equity",
        "tradingagent_market_data_freshness_seconds",
    ):
        assert name in metrics


def test_job_errors_are_classified_without_internal_details() -> None:
    record = JobRecord(
        id="job-1",
        kind="paper_cycle",
        status="failed",
        progress=10,
        retry_count=3,
        maximum_retries=3,
        payload={},
        error_class="sqlalchemy.exc.OperationalError",
        error_message="password=secret host=postgres traceback...",
        idempotency_key="job-1",
    )
    view = ApplicationRegistry._job_view(record)
    assert view.error_class == "internal_error"
    assert view.error_message == "Job failed"
    assert "secret" not in view.model_dump_json()
