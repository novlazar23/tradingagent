"""Safe read-only HTTP adapter for the OctoBot history service."""

import json
import random
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from tradingagent.domain.models import Candle

from .normalization import collection, normalize_candle, normalize_datasets
from .timeframes import Timeframe, timeframe_delta


@dataclass(frozen=True, slots=True)
class Response:
    """Transport-neutral HTTP response."""

    status: int
    body: bytes


class GetTransport(Protocol):
    """Minimal injectable GET-only transport used by contract tests."""

    def get(
        self, url: str, *, params: dict[str, str], headers: dict[str, str], timeout: float
    ) -> Response: ...


class UrllibGetTransport:
    """GET-only transport with redirects disabled and bounded response reads."""

    redirects_allowed = False

    class _RejectRedirects(HTTPRedirectHandler):
        def redirect_request(
            self,
            req: Request,
            fp: object,
            code: int,
            msg: str,
            headers: Message,
            newurl: str,
        ) -> None:
            del req, fp, code, msg, headers, newurl
            return None

    def __init__(self, *, max_response_bytes: int = 8 * 1024 * 1024) -> None:
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self.max_response_bytes = max_response_bytes
        self._opener = build_opener(self._RejectRedirects())

    def get(
        self, url: str, *, params: dict[str, str], headers: dict[str, str], timeout: float
    ) -> Response:
        target = f"{url}?{urlencode(params)}" if params else url
        request = Request(target, headers=headers, method="GET")
        with self._opener.open(request, timeout=timeout) as result:  # noqa: S310
            body = result.read(self.max_response_bytes + 1)
            if len(body) > self.max_response_bytes:
                raise RuntimeError("history response size limit exceeded")
            return Response(status=result.status, body=body)


class OctoBotHistoryClient:
    """Read datasets and native OHLCV candles from one fixed history host.

    The API key is read once from a file and is never included in exceptions.
    Retries are limited to network failures, HTTP 429, and server errors.
    Candle pagination advances the inclusive ``start`` parameter to the next
    native interval; a non-advancing page is rejected to prevent infinite loops.
    """

    def __init__(
        self,
        base_url: str,
        api_key_file: str | Path,
        *,
        transport: GetTransport | None = None,
        timeout: float = 10,
        max_retries: int = 3,
        max_response_bytes: int = 8 * 1024 * 1024,
        retry_delay: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("history base URL must be an exact http(s) origin")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("history base URL must be an exact http(s) origin") from exc
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        key = Path(api_key_file).read_text().strip()
        if not key:
            raise ValueError("history API key file is empty")
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        self._base_url = f"{parsed.scheme}://{host}:{port}"
        self._origin = (parsed.scheme, parsed.hostname, port)
        self._headers = {"X-API-Key": key}
        self._max_response_bytes = max_response_bytes
        self._transport = transport or UrllibGetTransport(max_response_bytes=max_response_bytes)
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_delay = retry_delay

    def _get(self, path: str, params: dict[str, str]) -> object:
        url = f"{self._base_url}{path}"
        for attempt in range(self._max_retries + 1):
            try:
                response = self._transport.get(
                    url, params=params, headers=self._headers, timeout=self._timeout
                )
                response_url = urlsplit(url)
                response_port = response_url.port or (443 if response_url.scheme == "https" else 80)
                if (
                    response_url.scheme,
                    response_url.hostname,
                    response_port,
                ) != self._origin:
                    raise RuntimeError("history request escaped configured origin")
                if len(response.body) > self._max_response_bytes:
                    raise RuntimeError("history response size limit exceeded")
                if response.status == 429 or response.status >= 500:
                    raise HTTPError(
                        url, response.status, "transient history error", Message(), None
                    )
                if response.status >= 400:
                    raise RuntimeError(f"history request failed with HTTP {response.status}")
                return json.loads(response.body)
            except (HTTPError, URLError, TimeoutError) as error:
                retryable = (
                    not isinstance(error, HTTPError) or error.code == 429 or error.code >= 500
                )
                if not retryable or attempt >= self._max_retries:
                    raise RuntimeError("history request failed") from error
                self._retry_delay((2**attempt) + random.uniform(0, 0.25))
        raise AssertionError("retry loop exhausted")

    def list_datasets(self) -> tuple[str, ...]:
        """Return available IDs; callers must choose a dataset explicitly."""
        return normalize_datasets(self._get("/api/v1/historical/datasets", {}))

    def iter_candles(
        self,
        *,
        dataset_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        limit: int,
        now: datetime,
    ) -> Iterator[Candle]:
        """Stream chronological pages for the requested half-open UTC range."""
        if symbol != "BTC/USDT":
            raise ValueError("only BTC/USDT is supported")
        if limit <= 0:
            raise ValueError("limit must be positive")
        cursor = start
        while cursor < end:
            payload = self._get(
                "/api/v1/historical/candles",
                {
                    "dataset": dataset_id,
                    "symbol": symbol,
                    "time_frame": timeframe,
                    "start": str(int(cursor.timestamp())),
                    "end": str(int(end.timestamp())),
                    "limit": str(limit),
                },
            )
            rows = collection(payload, "candles")
            if len(rows) > limit:
                raise RuntimeError("history response exceeded requested row limit")
            page = sorted(
                (
                    normalize_candle(
                        row,
                        dataset_id=dataset_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        now=now,
                    )
                    for row in rows
                ),
                key=lambda candle: candle.open_time,
            )
            for candle in page:
                if cursor <= candle.open_time < end:
                    yield candle
            if len(rows) < limit:
                return
            next_cursor = page[-1].open_time + timeframe_delta(timeframe)
            if next_cursor <= cursor:
                raise RuntimeError("history pagination made no progress")
            cursor = next_cursor
