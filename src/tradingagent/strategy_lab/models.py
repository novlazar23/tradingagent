"""Versioned, reviewable artifacts produced by the video strategy pipeline."""

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrategyStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"


class TranscriptSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=10_000)

    @field_validator("end_seconds")
    @classmethod
    def end_after_start(cls, value: float, info: object) -> float:
        start = info.data.get("start_seconds") if hasattr(info, "data") else None
        if isinstance(start, (int, float)) and value <= start:
            raise ValueError("end_seconds must be greater than start_seconds")
        return value


class StrategySpec(BaseModel):
    """Human-reviewable strategy DSL; it is data, never executable Python."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default="1", pattern=r"^1$")
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_ -]*$")
    source_url: str = Field(min_length=1, max_length=2048)
    symbol: str = Field(default="BTC/USDT", pattern=r"^[A-Z0-9]+/[A-Z0-9]+$")
    timeframe: str = Field(default="1h", pattern=r"^\d+[mhd]$")
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: StrategyStatus = StrategyStatus.DRAFT
    indicators: dict[str, dict[str, int | float | str]] = Field(default_factory=dict)
    entry_rules: tuple[str, ...] = Field(min_length=1)
    exit_rules: tuple[str, ...] = Field(min_length=1)
    risk: dict[str, Decimal | int | float | str] = Field(default_factory=dict)
    evidence: tuple[TranscriptSegment, ...] = ()
    assumptions: tuple[str, ...] = ()
