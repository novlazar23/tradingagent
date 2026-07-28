from fastapi.testclient import TestClient

from tradingagent.api.app import create_app
from tradingagent.api.services import ApplicationRegistry


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
