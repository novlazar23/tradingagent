"""Small local Whisper HTTP service for YouTube strategy transcription."""

import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from faster_whisper import WhisperModel
from pydantic import BaseModel, Field

app = FastAPI(title="tradingagent Whisper service", version="1")
_models: dict[str, WhisperModel] = {}


class TranscriptionRequest(BaseModel):
    url: str = Field(min_length=12, max_length=2048)
    model: str = Field(default="small", pattern=r"^(tiny|base|small|medium|large-v3)$")
    language: str | None = Field(default=None, min_length=2, max_length=8)


def _youtube_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname in {
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
    }


def _model(name: str) -> WhisperModel:
    if name not in _models:
        _models[name] = WhisperModel(
            name,
            device=os.environ.get("WHISPER_DEVICE", "cpu"),
            compute_type=os.environ.get("WHISPER_COMPUTE_TYPE", "int8"),
            download_root=os.environ.get("WHISPER_MODEL_DIR", "/models"),
        )
    return _models[name]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/transcribe")
def transcribe(request: TranscriptionRequest) -> dict[str, object]:
    if not _youtube_url(request.url):
        raise HTTPException(status_code=400, detail="only HTTPS YouTube URLs are accepted")
    with tempfile.TemporaryDirectory(prefix="tradingagent-whisper-") as directory:
        target = str(Path(directory) / "audio.%(ext)s")
        options = {
            "format": "bestaudio/best",
            "outtmpl": target,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": 500 * 1024 * 1024,
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([request.url])
        except Exception as exc:
            raise HTTPException(status_code=502, detail="YouTube audio download failed") from exc
        audio = next(Path(directory).glob("audio.*"), None)
        if audio is None:
            raise HTTPException(status_code=502, detail="YouTube audio was not produced")
        segments, info = _model(request.model).transcribe(str(audio), language=request.language)
        return {
            "language": info.language,
            "segments": [
                {"start": segment.start, "end": segment.end, "text": segment.text.strip()}
                for segment in segments
            ],
        }
