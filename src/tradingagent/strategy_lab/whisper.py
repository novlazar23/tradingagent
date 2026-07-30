"""HTTP contract for the local Whisper transcription service."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.request import Request, urlopen

from tradingagent.strategy_lab.models import TranscriptSegment


@dataclass(frozen=True, slots=True)
class Transcript:
    language: str | None
    segments: tuple[TranscriptSegment, ...]


class WhisperClient:
    def __init__(self, base_url: str, *, model: str = "small", timeout: int = 300) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def transcribe(self, url: str, *, language: str | None = None) -> Transcript:
        payload = json.dumps({"url": url, "model": self.model, "language": language}).encode()
        request = Request(
            f"{self.base_url}/transcribe",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            body = json.loads(response.read())
        if not isinstance(body, Mapping) or not isinstance(body.get("segments"), list):
            raise ValueError("Whisper response must contain a segments list")
        return Transcript(
            language=str(body["language"]) if body.get("language") else None,
            segments=tuple(
                TranscriptSegment(
                    start_seconds=float(segment["start"]),
                    end_seconds=float(segment["end"]),
                    text=str(segment["text"]).strip(),
                )
                for segment in body["segments"]
            ),
        )
