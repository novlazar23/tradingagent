import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_compose_declares_separate_mandatory_services_and_secret() -> None:
    compose = (ROOT / "compose.yaml").read_text()

    for service in ("api:", "worker:", "scheduler:", "postgres:"):
        assert service in compose
    assert "octobot_history_api_key" in compose
    assert "read_only: true" in compose


def test_initial_migration_is_present() -> None:
    migrations = list((ROOT / "migrations" / "versions").glob("*.py"))

    assert len(migrations) == 1
    assert "configuration_snapshots" in migrations[0].read_text()


def test_container_entrypoint_exposes_service_roles() -> None:
    result = subprocess.run(
        ["tradingagent", "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    for role in ("serve-api", "run-worker", "run-scheduler"):
        assert role in result.stdout
