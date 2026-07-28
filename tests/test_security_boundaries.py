from pathlib import Path

from fastapi.testclient import TestClient

from tradingagent.api.app import create_app

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


def test_image_contains_no_history_secret() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "history_api_key" not in dockerfile.lower()
    assert "USER 10001:10001" in dockerfile
