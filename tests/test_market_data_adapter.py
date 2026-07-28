from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError

import pytest

from tradingagent.market_data.adapter import OctoBotHistoryClient, Response


class ScriptedTransport:
    def __init__(self, responses: list[Response | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def get(
        self, url: str, *, params: dict[str, str], headers: dict[str, str], timeout: float
    ) -> Response:
        self.calls.append((url, params, headers))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def response(payload: object, status: int = 200) -> Response:
    return Response(status=status, body=json.dumps(payload).encode())


def test_client_loads_secret_and_normalizes_dataset_envelopes(tmp_path: Path) -> None:
    secret = tmp_path / "key"
    secret.write_text("dummy-key\n")
    transport = ScriptedTransport([response({"data": {"datasets": [{"name": "dataset.data"}]}})])
    client = OctoBotHistoryClient("http://history:5002", secret, transport=transport)

    assert client.list_datasets() == ("dataset.data",)
    url, params, headers = transport.calls[0]
    assert url == "http://history:5002/api/v1/historical/datasets"
    assert params == {}
    assert headers == {"X-API-Key": "dummy-key"}


def test_client_retries_transient_status_then_paginates_by_time(tmp_path: Path) -> None:
    secret = tmp_path / "key"
    secret.write_text("not-a-real-secret")
    first_page = [
        [0, "100", "110", "90", "105", "5"],
        [900, "105", "112", "101", "108", "6"],
    ]
    second_page = [[1800, "108", "115", "107", "114", "7"]]
    transient = HTTPError("url", 503, "temporary", {}, None)
    transport = ScriptedTransport(
        [transient, response({"candles": first_page}), response({"data": second_page})]
    )
    client = OctoBotHistoryClient(
        "http://history:5002",
        secret,
        transport=transport,
        max_retries=1,
        retry_delay=lambda _seconds: None,
    )

    candles = list(
        client.iter_candles(
            dataset_id="dataset.data",
            symbol="BTC/USDT",
            timeframe="15m",
            start=datetime(1970, 1, 1, tzinfo=UTC),
            end=datetime(1970, 1, 1, 0, 45, tzinfo=UTC),
            limit=2,
            now=datetime(1970, 1, 2, tzinfo=UTC),
        )
    )

    assert [c.open_time.timestamp() for c in candles] == [0, 900, 1800]
    assert transport.calls[-1][1]["start"] == "1800"
    assert all(call[1]["time_frame"] == "15m" for call in transport.calls[1:])


def test_client_rejects_empty_secret(tmp_path: Path) -> None:
    secret = tmp_path / "key"
    secret.write_text("\n")

    with pytest.raises(ValueError, match="empty"):
        OctoBotHistoryClient("http://history:5002", secret)
