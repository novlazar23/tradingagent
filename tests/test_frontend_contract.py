from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_frontend_bundle_contains_control_plane_and_safe_api_proxy() -> None:
    index = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    nginx = (ROOT / "infra" / "frontend.conf").read_text(encoding="utf-8")

    for label in ("Dashboard", "Backtest", "Paper-Trading", "Strategie", "Daten"):
        assert label in index
    for route in (
        "/health/ready",
        "/api/v1/backtests",
        "/api/v1/paper-sessions",
        "/api/v1/strategies/validate",
    ):
        assert route in script
    assert "proxy_pass http://api:8000" in nginx
    assert "X-Frame-Options" in nginx


def test_compose_exposes_frontend_only_on_loopback() -> None:
    config = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    frontend = config["services"]["frontend"]
    assert frontend["ports"] == ["127.0.0.1:${FRONTEND_PORT:-8080}:8080"]
    assert frontend["networks"] == ["backend"]
    assert frontend["read_only"] is True
