"""Idempotent candle import, revision audit, and gap detection services."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from tradingagent.domain.models import Candle

from .timeframes import Timeframe, timeframe_delta

CandleKey = tuple[str, str, str, str, datetime]


def _key(candle: Candle) -> CandleKey:
    return (
        candle.source,
        candle.dataset_id,
        candle.symbol,
        candle.timeframe,
        candle.open_time,
    )


@dataclass(frozen=True, slots=True)
class CandleRevision:
    """Auditable previous and replacement source values."""

    before: Candle
    after: Candle


@dataclass(frozen=True, slots=True)
class DataGap:
    """One expected but absent native candle interval."""

    source: str
    dataset_id: str
    symbol: str
    timeframe: Timeframe
    open_time: datetime
    close_time: datetime


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Counts from an idempotent import operation."""

    inserted: int = 0
    revised: int = 0
    unchanged: int = 0


class CandleRepository(Protocol):
    """Persistence port required by :class:`MarketDataService`."""

    revisions: list[CandleRevision]
    gaps: list[DataGap]

    def get(self, key: CandleKey) -> Candle | None: ...
    def put(self, candle: Candle) -> None: ...
    def list_range(
        self,
        source: str,
        dataset_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> tuple[Candle, ...]: ...


class InMemoryCandleRepository:
    """Deterministic repository for unit tests and local domain composition."""

    def __init__(self) -> None:
        self.candles: dict[CandleKey, Candle] = {}
        self.revisions: list[CandleRevision] = []
        self.gaps: list[DataGap] = []

    def get(self, key: CandleKey) -> Candle | None:
        return self.candles.get(key)

    def put(self, candle: Candle) -> None:
        self.candles[_key(candle)] = candle

    def list_range(
        self,
        source: str,
        dataset_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> tuple[Candle, ...]:
        return tuple(
            sorted(
                (
                    candle
                    for candle in self.candles.values()
                    if candle.source == source
                    and candle.dataset_id == dataset_id
                    and candle.symbol == symbol
                    and candle.timeframe == timeframe
                    and start <= candle.open_time < end
                ),
                key=lambda candle: candle.open_time,
            )
        )


class MarketDataService:
    """Application service for normalized market-data persistence."""

    def __init__(self, repository: CandleRepository) -> None:
        self._repository = repository

    def import_candles(self, candles: Iterable[Candle]) -> ImportResult:
        """Insert new candles and audit source revisions idempotently."""
        inserted = revised = unchanged = 0
        for candle in candles:
            existing = self._repository.get(_key(candle))
            if existing is None:
                self._repository.put(candle)
                inserted += 1
            elif existing.source_fingerprint == candle.source_fingerprint:
                unchanged += 1
            else:
                self._repository.revisions.append(CandleRevision(existing, candle))
                self._repository.put(candle)
                revised += 1
        return ImportResult(inserted, revised, unchanged)

    def detect_gaps(
        self,
        *,
        source: str,
        dataset_id: str,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> tuple[DataGap, ...]:
        """Persist and return absent native intervals in a half-open range."""
        duration = timeframe_delta(timeframe)
        present = {
            candle.open_time
            for candle in self._repository.list_range(
                source, dataset_id, symbol, timeframe, start, end
            )
        }
        detected: list[DataGap] = []
        expected = start
        while expected < end:
            if expected not in present:
                gap = DataGap(source, dataset_id, symbol, timeframe, expected, expected + duration)
                if gap not in self._repository.gaps:
                    self._repository.gaps.append(gap)
                detected.append(gap)
            expected += duration
        return tuple(detected)

    def has_gap(self, timeframe: Timeframe, start: datetime, end: datetime) -> bool:
        """Return whether a persisted gap overlaps a decision window."""
        return any(
            gap.timeframe == timeframe and gap.open_time < end and gap.close_time > start
            for gap in self._repository.gaps
        )
