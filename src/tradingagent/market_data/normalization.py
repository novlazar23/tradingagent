"""Normalization for explicitly supported OctoBot history response variants.

Supported collection envelopes are a bare JSON list, ``{"datasets": [...]}``
or ``{"candles": [...]}``, ``{"data": [...]}``, and one nested
``{"data": {"datasets"|"candles": [...]}}`` level. Candle rows may be the
common six-element ``[timestamp, open, high, low, close, volume]`` sequence or
an object using ``timestamp``, ``time`` or ``open_time``.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from tradingagent.domain.models import Candle

from .timeframes import Timeframe, timeframe_delta


def collection(payload: object, name: str) -> list[object]:
    """Extract a named collection from a documented envelope variant."""
    candidate = payload
    if isinstance(candidate, Mapping):
        candidate = candidate.get(name, candidate.get("data"))
        if isinstance(candidate, Mapping):
            candidate = candidate.get(name)
    if not isinstance(candidate, list):
        raise ValueError(f"response does not contain a {name} list")
    return candidate


def normalize_datasets(payload: object) -> tuple[str, ...]:
    """Return dataset identifiers without selecting one implicitly."""
    result: list[str] = []
    for item in collection(payload, "datasets"):
        if isinstance(item, str):
            identifier = item
        elif isinstance(item, Mapping):
            raw = item.get("id", item.get("name", item.get("dataset")))
            if not isinstance(raw, str):
                raise ValueError("dataset object has no string id/name/dataset")
            identifier = raw
        else:
            raise ValueError("dataset must be a string or object")
        if not identifier:
            raise ValueError("dataset identifier cannot be empty")
        result.append(identifier)
    return tuple(result)


def _timestamp(value: object) -> datetime:
    if isinstance(value, str):
        try:
            numeric = Decimal(value)
        except InvalidOperation:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
                raise ValueError("candle timestamp must be UTC") from None
            return parsed
    elif isinstance(value, (int, float, Decimal)):
        numeric = Decimal(str(value))
    else:
        raise ValueError("invalid candle timestamp")
    if numeric >= Decimal("100000000000"):
        numeric /= 1000
    return datetime.fromtimestamp(float(numeric), tz=UTC)


def normalize_candle(
    row: object,
    *,
    dataset_id: str,
    symbol: str,
    timeframe: Timeframe,
    now: datetime,
) -> Candle:
    """Normalize and validate one external OHLCV row."""
    if isinstance(row, Mapping):
        timestamp = row.get("open_time", row.get("timestamp", row.get("time")))
        values = (
            timestamp,
            row.get("open"),
            row.get("high"),
            row.get("low"),
            row.get("close"),
            row.get("volume"),
        )
        explicit_close = row.get("close_time")
        source_closed = row.get("is_closed")
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and len(row) >= 6:
        values = tuple(row[:6])
        explicit_close = None
        source_closed = None
    else:
        raise ValueError("candle must be an object or six-element sequence")
    if any(value is None for value in values):
        raise ValueError("candle is missing a required OHLCV field")
    opened = _timestamp(values[0])
    closed = (
        _timestamp(explicit_close)
        if explicit_close is not None
        else opened + timeframe_delta(timeframe)
    )
    fingerprint = hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    inferred_closed = closed <= now
    return Candle(
        source="octobot",
        dataset_id=dataset_id,
        symbol=symbol,  # type: ignore[arg-type]
        timeframe=timeframe,
        open_time=opened,
        close_time=closed,
        open=Decimal(str(values[1])),
        high=Decimal(str(values[2])),
        low=Decimal(str(values[3])),
        close=Decimal(str(values[4])),
        volume=Decimal(str(values[5])),
        is_closed=(
            inferred_closed if source_closed is None else bool(source_closed) and inferred_closed
        ),
        source_fingerprint=fingerprint,
        ingested_at=now,
    )
