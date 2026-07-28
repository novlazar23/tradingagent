"""Minimal JSON logging bootstrap with an explicit safe-field allow-list."""

import json
import logging
from datetime import UTC, datetime

_SAFE_FIELDS = (
    "event",
    "correlation_id",
    "job_id",
    "session_id",
    "method",
    "path",
    "status",
    "kind",
    "duration_seconds",
)


class JsonFormatter(logging.Formatter):
    """Serialize operational logs without copying arbitrary extras or secrets."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        for field in _SAFE_FIELDS:
            if field != "event" and hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            exception_type = record.exc_info[0]
            if exception_type is not None:
                payload["exception_class"] = exception_type.__name__
        return json.dumps(payload, separators=(",", ":"), default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install deterministic structured logging for every process role."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
