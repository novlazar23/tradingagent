from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradingagent.domain.models import Candle
from tradingagent.strategy_lab.freqtrade import export_ohlcv_json


def candle() -> Candle:
    opened = datetime(2026, 1, 1, tzinfo=UTC)
    return Candle(
        source="octobot",
        dataset_id="dataset",
        symbol="BTC/USDT",
        timeframe="1h",
        open_time=opened,
        close_time=opened + timedelta(hours=1),
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("12.5"),
        is_closed=True,
        source_fingerprint="fingerprint",
        ingested_at=opened,
    )


def test_export_ohlcv_json_uses_freqtrade_timestamp_and_pair_conventions(tmp_path) -> None:
    path = export_ohlcv_json(tmp_path, (candle(),))

    assert path.name == "BTC_USDT-1h.json"
    assert path.parent == tmp_path
    assert path.read_text() == "[[1767225600000,100.0,102.0,99.0,101.0,12.5]]\n"
