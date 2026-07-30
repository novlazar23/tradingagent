import json

from tradingagent.strategy_lab.whisper import WhisperClient


def test_whisper_client_returns_transcript_segments(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class Response:
        def read(self) -> bytes:
            return json.dumps(
                {
                    "language": "de",
                    "segments": [{"start": 1.0, "end": 2.5, "text": "RSI unter 30 kaufen"}],
                }
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def open_url(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr("tradingagent.strategy_lab.whisper.urlopen", open_url)
    result = WhisperClient("http://whisper:8080").transcribe("https://youtu.be/example")

    assert seen == {
        "url": "http://whisper:8080/transcribe",
        "body": {"url": "https://youtu.be/example", "model": "small", "language": None},
        "timeout": 300,
    }
    assert result.language == "de"
    assert result.segments[0].text == "RSI unter 30 kaufen"
