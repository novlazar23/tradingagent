from datetime import datetime
from decimal import Decimal

import pytest

from tradingagent.domain.models import Candle
from tradingagent.market_data.timeframes import timeframe_delta


@pytest.fixture
def candle_factory():
    def make(timeframe: str, opened: datetime, *, is_closed: bool) -> Candle:
        return Candle(
            source="octobot",
            dataset_id="d",
            symbol="BTC/USDT",
            timeframe=timeframe,  # type: ignore[arg-type]
            open_time=opened,
            close_time=opened + timeframe_delta(timeframe),  # type: ignore[arg-type]
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=Decimal("5"),
            is_closed=is_closed,
            source_fingerprint="x",
            ingested_at=opened,
        )

    return make
