"""Versioned, deliberately small HTTP transport models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    """Reject unversioned or accidental request fields."""

    model_config = ConfigDict(extra="forbid")


class ErrorDetail(ApiModel):
    code: str
    message: str
    correlation_id: str
    details: dict[str, object] | None = None


class ErrorResponse(ApiModel):
    schema_version: Literal["1"] = "1"
    error: ErrorDetail


class DatasetList(ApiModel):
    datasets: list[dict[str, object]]


class OperationRequest(ApiModel):
    dataset_id: str = Field(min_length=1, max_length=255)
    configuration_version: str | None = Field(default=None, min_length=1, max_length=255)


class StrategyValidationRequest(ApiModel):
    configuration: dict[str, object]


class AcceptedOperation(ApiModel):
    resource_id: str
    resource_url: str
    job_id: str
    job_url: str


class JobView(ApiModel):
    id: str
    kind: str
    status: Literal["queued", "running", "completed", "failed"]
    progress: int = Field(ge=0, le=100)
    retry_count: int = Field(ge=0)
    error_class: str | None = None
    error_message: str | None = None


class ResourceView(ApiModel):
    id: str
    kind: Literal["backtest", "paper_session"]
    state: str
    request: dict[str, object]


class HealthView(ApiModel):
    status: Literal["ok", "degraded", "not_ready"]
    dependencies: dict[str, str] | None = None
