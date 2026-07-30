"""Deterministic OctoBot history stub used only by the Compose CI workflow."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

INTERVALS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        target = urlsplit(self.path)
        if self.headers.get("X-API-Key") != "history-integration-only":
            self.send_error(401)
            return
        if target.path == "/api/v1/historical/datasets":
            self._json({"datasets": ["ci-btc.data"]})
            return
        if target.path != "/api/v1/historical/candles":
            self.send_error(404)
            return
        query = parse_qs(target.query)
        timeframe = query["time_frame"][0]
        step = INTERVALS[timeframe]
        start, end = int(query["start"][0]), int(query["end"][0])
        limit = int(query["limit"][0])
        rows = []
        for index, timestamp in enumerate(range(start, end, step)):
            if len(rows) == limit:
                break
            price = 100_000 + (timestamp // 900) % 1_000 + index
            rows.append([timestamp, price, price + 20, price - 20, price + 5, 10])
        self._json({"candles": rows})

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _json(self, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("0.0.0.0", 5002), Handler).serve_forever()
