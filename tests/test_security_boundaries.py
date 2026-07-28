import json
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from tradingagent.api.app import create_app
from tradingagent.logging import configure_logging

ROOT = Path(__file__).parents[1]


def test_api_has_no_live_trading_or_order_endpoint() -> None:
    schema = TestClient(create_app()).get("/openapi.json").json()
    paths = " ".join(schema["paths"]).lower()
    assert "/orders" not in paths
    assert "live-trading" not in paths
    assert "exchange-order" not in paths


def test_compose_keeps_database_private_and_containers_hardened() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    postgres = compose.split("\n  postgres:\n", 1)[1].split("\nsecrets:", 1)[0]
    assert "ports:" not in postgres
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose
    assert "internal: true" in compose
    assert "OCTOBOT_HISTORY_API_KEY:" not in compose
    assert 'user: "70:70"' in postgres
    assert "cap_add:" not in postgres


def test_image_contains_no_history_secret() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "history_api_key" not in dockerfile.lower()
    assert "USER 10001:10001" in dockerfile


def test_no_plaintext_database_url_contract() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    config_source = (ROOT / "src/tradingagent/config.py").read_text()
    migration_source = (ROOT / "migrations/env.py").read_text()

    assert "DATABASE_URL:" not in compose
    assert 'os.getenv("DATABASE_URL")' not in config_source
    assert 'os.getenv("DATABASE_URL")' not in migration_source


def test_runtime_logging_does_not_copy_messages_or_arbitrary_extras(
    capsys: object,
) -> None:
    configure_logging()
    logging.getLogger("tradingagent.security").error(
        "password=must-not-leak",
        extra={"api_key": "must-not-leak", "event": "safe_failure"},
    )

    payload = json.loads(capsys.readouterr().err)  # type: ignore[attr-defined]
    assert payload["event"] == "safe_failure"
    assert "must-not-leak" not in json.dumps(payload)


def test_only_history_proxy_has_external_egress() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    application = compose.split("services:", 1)[1].split("\n  history-proxy:", 1)[0]
    proxy = compose.split("\n  history-proxy:", 1)[1].split("\n  postgres:", 1)[0]

    assert "history-egress" not in application
    assert "history-egress" in proxy
