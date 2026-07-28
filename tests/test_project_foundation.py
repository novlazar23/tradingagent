import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from tradingagent.api.app import create_app
from tradingagent.logging import configure_logging

ROOT = Path(__file__).parents[1]


def test_compose_declares_separate_mandatory_services_and_secret() -> None:
    compose = (ROOT / "compose.yaml").read_text()

    for service in ("api:", "worker:", "scheduler:", "postgres:"):
        assert service in compose
    assert "octobot_history_api_key" in compose
    assert "read_only: true" in compose


def test_compose_uses_secret_only_database_password_and_migration_gate() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    assert "POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password" in compose
    assert "DATABASE_PASSWORD_FILE: /run/secrets/postgres_password" in compose
    assert "POSTGRES_PASSWORD:" not in compose
    assert "DATABASE_URL:" not in compose
    assert "migrate:" in compose
    assert "condition: service_completed_successfully" in compose


def test_history_proxy_runs_unprivileged_without_capabilities() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    proxy = compose.split("\n  history-proxy:\n", 1)[1].split("\n  postgres:\n", 1)[0]
    assert 'user: "101:101"' in proxy
    assert "cap_drop:\n      - ALL" in proxy
    assert "cap_add:" not in proxy


def test_compose_isolates_apps_behind_fixed_history_proxy() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    proxy = (ROOT / "infra" / "history-proxy.conf").read_text()

    assert "history-proxy:" in compose
    assert "history-client:" in compose
    assert "internal: true" in compose
    assert "OCTOBOT_HISTORY_BASE_URL: http://history-proxy:8080" in compose
    assert "192.168.178.20:5002" in proxy
    assert "location = /health" in proxy
    assert "location = /api/v1/historical/candles" in proxy
    assert "location /" in proxy and "return 404" in proxy


def test_container_inputs_are_digest_pinned_and_dependency_install_is_frozen() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "compose.yaml").read_text()

    assert "python:3.12-alpine3.22@sha256:" in dockerfile
    assert "RUN apk upgrade --no-cache" in dockerfile
    assert "uv sync --frozen" in dockerfile
    assert "postgres:17-alpine@sha256:" in compose
    assert "nginx:1.29-alpine@sha256:" in compose


def test_initial_migration_is_present() -> None:
    migrations = list((ROOT / "migrations" / "versions").glob("*.py"))

    assert len(migrations) == 1
    source = migrations[0].read_text()
    assert "op.create_table" in source
    assert "op.create_index" in source
    assert "Base" not in source
    assert "tradingagent.persistence.models" not in source
    assert '"claim_generation"' in source
    assert '"ix_candles_closed_close"' in source


def test_compose_mounts_secrets_with_least_privilege_metadata() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    api = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
    worker = compose.split("\n  worker:\n", 1)[1].split("\n  scheduler:\n", 1)[0]
    assert "source: octobot_history_api_key" in compose
    assert "source: octobot_history_api_key" not in api
    assert "source: octobot_history_api_key" in worker
    assert "source: postgres_password" in compose
    assert 'uid: "10001"' in compose
    assert 'uid: "70"' in compose
    assert "mode: 0400" in compose


def test_read_only_history_proxy_has_bounded_writable_cache() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    proxy = compose.split("\n  history-proxy:\n", 1)[1].split("\n  postgres:\n", 1)[0]

    assert "/var/cache/nginx:size=16m" in proxy


def test_compose_e2e_uses_the_application_virtual_environment() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    e2e = (ROOT / "infra" / "ci_compose_workflow.py").read_text()

    assert "exec -T api /app/.venv/bin/python /tmp/ci_compose_workflow.py" in workflow
    assert "exec -T api /app/.venv/bin/python -c" in workflow
    assert "curl --fail --silent http://127.0.0.1:8000/health/ready" not in workflow
    assert "replace(hour=0, minute=0, second=0, microsecond=0)" in e2e


def test_compose_renders_as_a_static_deployment_contract(tmp_path: Path) -> None:
    history_secret = tmp_path / "history_api_key"
    history_secret.write_text("compose-validation-only")
    postgres_secret = tmp_path / "postgres_password"
    postgres_secret.write_text("compose-validation-only")
    environment = {
        **os.environ,
        "POSTGRES_PASSWORD": "compose-validation-only",
        "OCTOBOT_HISTORY_API_KEY_FILE": str(history_secret),
        "POSTGRES_PASSWORD_FILE": str(postgres_secret),
    }
    result = subprocess.run(
        ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "config", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr


def test_generated_openapi_is_deterministic_and_valid_json() -> None:
    api = TestClient(create_app())
    first = api.get("/openapi.json")
    second = api.get("/openapi.json")

    assert first.status_code == second.status_code == 200
    assert json.loads(first.text) == json.loads(second.text)


def test_container_entrypoint_exposes_service_roles() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tradingagent.cli import main; main()",
            "--help",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    for role in ("serve-api", "run-worker", "run-scheduler"):
        assert role in result.stdout


def test_logging_bootstrap_emits_structured_json(capsys: object) -> None:
    configure_logging()
    logging.getLogger("tradingagent.test").info(
        "safe_event", extra={"event": "safe_event", "job_id": "job-1"}
    )
    payload = json.loads(capsys.readouterr().err)  # type: ignore[attr-defined]
    assert payload["level"] == "INFO"
    assert payload["event"] == "safe_event"
    assert payload["job_id"] == "job-1"


def test_ci_gates_quality_contract_integration_security_and_sbom() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for gate in (
        "ruff format --check",
        "ruff check",
        "mypy",
        "pytest",
        "docker compose config",
        "pip-audit",
        "trivy",
        "syft",
        "alembic downgrade base",
        "alembic upgrade head",
    ):
        assert gate in workflow
    assert "ignore-unfixed" not in workflow
