"""Conservative first-pass extraction from Whisper transcript segments."""

import hashlib
import json
import re
from collections.abc import Iterable

from tradingagent.strategy_lab.models import StrategySpec, StrategyStatus, TranscriptSegment

_RSI = re.compile(
    r"rsi\D+(?:below|under|unter|below)\D+(\d+(?:\.\d+)?)|rsi\D+(?:above|over|über|ueber)\D+(\d+(?:\.\d+)?)",
    re.I,
)


def extract_strategy(
    *, name: str, source_url: str, segments: Iterable[TranscriptSegment], timeframe: str
) -> StrategySpec:
    selected = tuple(segments)
    entry: list[str] = []
    exit_: list[str] = []
    evidence: list[TranscriptSegment] = []
    for segment in selected:
        text = segment.text.lower()
        match = _RSI.search(text)
        if match is None:
            continue
        threshold = next(value for value in match.groups() if value is not None)
        if any(word in text for word in ("buy", "entry", "kauf", "long")):
            entry.append(f"rsi < {threshold}")
            evidence.append(segment)
        elif any(word in text for word in ("sell", "exit", "verkauf", "close")):
            exit_.append(f"rsi > {threshold}")
            evidence.append(segment)
    if not entry or not exit_:
        raise ValueError("could not extract both an entry and an exit rule")
    transcript_hash = hashlib.sha256(
        json.dumps([segment.model_dump() for segment in selected], sort_keys=True).encode()
    ).hexdigest()
    return StrategySpec(
        name=name,
        source_url=source_url,
        timeframe=timeframe,
        transcript_sha256=transcript_hash,
        status=StrategyStatus.DRAFT,
        indicators={"rsi": {"period": 14}},
        entry_rules=tuple(dict.fromkeys(entry)),
        exit_rules=tuple(dict.fromkeys(exit_)),
        evidence=tuple(evidence),
        assumptions=("RSI period defaults to 14 because the video did not specify it.",),
    )
