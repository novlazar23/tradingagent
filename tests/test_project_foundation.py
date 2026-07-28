import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_compose_declares_separate_mandatory_services_and_secret() -> None:
    compose = (ROOT / "compose.yaml").read_text()

    for service in ("api:", "worker:", "scheduler:", "postgres:"):
        assert service in compose
    assert "octobot_history_api_key" in compose
    assert "read_only: true" in compose


def test_compose_isolates_apps_behind_fixed_history_proxy() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    proxy = (ROOT / "infra" / "history-proxy.conf").read_text()

    assert "history-proxy:" in compose
    assert "history-client:" in compose
    assert "internal: true" in compose
    assert "OCTOBOT_HISTORY_BASE_URL: http://history-proxy:8080" in compose
    assert "192.168.178.20:5002" in proxy
    assert "location = /api/v1/historical/candles" in proxy
    assert "location /" in proxy and "return 404" in proxy


def test_container_inputs_are_digest_pinned_and_dependency_install_is_frozen() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "compose.yaml").read_text()

    assert "python:3.12-slim@sha256:" in dockerfile
    assert "uv sync --frozen" in dockerfile
    assert "postgres:17-alpine@sha256:" in compose
    assert "nginx:1.29-alpine@sha256:" in compose


def test_initial_migration_is_present() -> None:
    migrations = list((ROOT / "migrations" / "versions").glob("*.py"))

    assert len(migrations) == 1
    assert "Base.metadata.create_all" in migrations[0].read_text()


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
