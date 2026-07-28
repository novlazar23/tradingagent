"""Decimal-safe and UTC-only core domain value objects."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError(f"{field} must be timezone-aware UTC")


@dataclass(frozen=True, slots=True)
class Candle:
    """Normalized, source-addressable OHLCV candle."""

    source: str
    dataset_id: str
    symbol: Literal["BTC/USDT"]
    timeframe: Literal["15m", "1h", "4h", "1d"]
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool
    source_fingerprint: str
    ingested_at: datetime

    def __post_init__(self) -> None:
        _require_utc(self.open_time, "open_time")
        _require_utc(self.close_time, "close_time")
        _require_utc(self.ingested_at, "ingested_at")
        if self.close_time <= self.open_time:
            raise ValueError("close_time must be after open_time")
        timeframe_seconds = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[self.timeframe]
        if int(self.open_time.timestamp()) % timeframe_seconds != 0:
            raise ValueError("open_time must be aligned to its UTC timeframe")
        if self.close_time.timestamp() - self.open_time.timestamp() != timeframe_seconds:
            raise ValueError("close_time must match the native timeframe duration")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("prices must be positive")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC values violate high/low bounds")


@dataclass(frozen=True, slots=True)
class Portfolio:
    """Long/flat BTC/USDT balances represented exclusively as Decimal."""

    cash: Decimal
    btc: Decimal

    def __post_init__(self) -> None:
        if self.cash < 0 or self.btc < 0:
            raise ValueError("portfolio balances cannot be negative")

    def equity(self, btc_price: Decimal) -> Decimal:
        """Return USDT equity at a strictly positive BTC mark price.

        Args:
            btc_price: BTC price expressed in USDT.

        Returns:
            Cash plus marked-to-market BTC value.

        Raises:
            ValueError: If the mark price is not positive.
        """
        if btc_price <= 0:
            raise ValueError("btc_price must be positive")
        return self.cash + self.btc * btc_price


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """Immutable signed asset movement linked to a domain event."""

    entry_type: Literal["deposit", "trade", "fee", "adjustment"]
    asset: Literal["BTC", "USDT"]
    amount: Decimal
    occurred_at: datetime
    reference_id: str

    def __post_init__(self) -> None:
        _require_utc(self.occurred_at, "occurred_at")
        if self.amount == 0:
            raise ValueError("ledger amount must be non-zero")
        if not self.reference_id:
            raise ValueError("reference_id cannot be empty")
