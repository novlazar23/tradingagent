from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradingagent.domain.models import Candle
from tradingagent.market_data.service import InMemoryCandleRepository, MarketDataService


def candle(open_minute: int, *, close: str = "105", fingerprint: str = "a") -> Candle:
    opened = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=open_minute)
    return Candle(
        source="octobot",
        dataset_id="d",
        symbol="BTC/USDT",
        timeframe="15m",
        open_time=opened,
        close_time=opened + timedelta(minutes=15),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal(close),
        volume=Decimal("5"),
        is_closed=True,
        source_fingerprint=fingerprint,
        ingested_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_upsert_is_idempotent_and_records_exactly_one_revision() -> None:
    repository = InMemoryCandleRepository()
    service = MarketDataService(repository)

    assert service.import_candles([candle(0)]).inserted == 1
    assert service.import_candles([candle(0)]).unchanged == 1
    revised = service.import_candles([candle(0, close="106", fingerprint="b")])
    assert revised.revised == 1
    assert len(repository.revisions) == 1
    assert repository.revisions[0].before.close == Decimal("105")
    assert repository.revisions[0].after.close == Decimal("106")


def test_gap_detection_finds_missing_interval_and_blocks_window() -> None:
    repository = InMemoryCandleRepository()
    service = MarketDataService(repository)
    service.import_candles([candle(0), candle(30)])

    gaps = service.detect_gaps(
        source="octobot",
        dataset_id="d",
        symbol="BTC/USDT",
        timeframe="15m",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 45, tzinfo=UTC),
    )

    assert [gap.open_time.minute for gap in gaps] == [15]
    assert service.has_gap(
        "15m",
        datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
    )


def test_domain_rejects_misaligned_candle() -> None:
    with pytest.raises(ValueError, match="aligned"):
        candle(1)
