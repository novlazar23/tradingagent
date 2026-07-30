from tradingagent.strategy_lab.extractor import extract_strategy
from tradingagent.strategy_lab.models import StrategyStatus, TranscriptSegment


def test_extractor_creates_draft_rules_with_transcript_evidence() -> None:
    transcript = (
        TranscriptSegment(start_seconds=10, end_seconds=15, text="Buy when RSI is below 30."),
        TranscriptSegment(start_seconds=30, end_seconds=35, text="Sell when RSI is above 70."),
    )

    spec = extract_strategy(
        name="rsi_video",
        source_url="https://youtu.be/example",
        segments=transcript,
        timeframe="1h",
    )

    assert spec.status is StrategyStatus.DRAFT
    assert spec.entry_rules == ("rsi < 30",)
    assert spec.exit_rules == ("rsi > 70",)
    assert spec.indicators == {"rsi": {"period": 14}}
    assert tuple(segment.start_seconds for segment in spec.evidence) == (10, 30)
