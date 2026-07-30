from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradingagent.domain.models import Candle, LedgerEntry, Portfolio


def test_candle_requires_utc_and_valid_ohlcv() -> None:
    with pytest.raises(ValueError, match="UTC"):
        Candle(
            source="octobot",
            dataset_id="fixture",
            symbol="BTC/USDT",
            timeframe="15m",
            open_time=datetime(2026, 1, 1),
            close_time=datetime(2026, 1, 1, 0, 15),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=Decimal("1"),
            is_closed=True,
            source_fingerprint="sha256:test",
            ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_portfolio_is_long_flat_and_uses_decimal() -> None:
    portfolio = Portfolio(cash=Decimal("1000"), btc=Decimal("0.1"))

    assert portfolio.equity(Decimal("50000")) == Decimal("6000.0")
    with pytest.raises(ValueError, match="negative"):
        Portfolio(cash=Decimal("1"), btc=Decimal("-0.1"))


def test_ledger_entry_requires_nonzero_decimal_amount_and_utc_time() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        LedgerEntry(
            entry_type="fee",
            asset="USDT",
            amount=Decimal("0"),
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            reference_id="fill-1",
        )
