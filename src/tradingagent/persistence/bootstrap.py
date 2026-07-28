"""Explicit first-run registration of OctoBot data and strategy lineage."""

import uuid
from datetime import UTC, datetime
from typing import Protocol

from tradingagent.api.services import ApplicationRegistry
from tradingagent.config import AppConfig
from tradingagent.persistence.models import MarketDataset
from tradingagent.persistence.snapshots import SnapshotResolver


class DatasetCatalog(Protocol):
    def list_datasets(self) -> tuple[str, ...]: ...


def bootstrap_environment(
    registry: ApplicationRegistry,
    history: DatasetCatalog,
    config: AppConfig,
    *,
    external_dataset_id: str,
    start: datetime,
    end: datetime,
    code_version: str,
) -> dict[str, str]:
    """Register one explicitly selected dataset and immutable configuration.

    Args:
        registry: PostgreSQL-backed application repository.
        history: Read-only OctoBot dataset catalog.
        config: Validated deployment, cost, risk, and strategy configuration.
        external_dataset_id: Exact OctoBot dataset identifier chosen by the operator.
        start: Inclusive UTC start of the import range.
        end: Exclusive UTC end of the import range.
        code_version: Deployed code identifier included in run lineage.

    Returns:
        Stable internal dataset ID and configuration fingerprint for later commands.

    Raises:
        ValueError: If the range is invalid or the dataset is unavailable.
    """
    if start.tzinfo is None or start.utcoffset() != UTC.utcoffset(start):
        raise ValueError("start must be timezone-aware UTC")
    if end.tzinfo is None or end.utcoffset() != UTC.utcoffset(end):
        raise ValueError("end must be timezone-aware UTC")
    if end <= start:
        raise ValueError("end must be after start")
    if external_dataset_id not in history.list_datasets():
        raise ValueError(f"OctoBot dataset is not available: {external_dataset_id}")

    dataset_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"octobot:{external_dataset_id}:BTC/USDT"))
    now = datetime.now(UTC)
    with registry.sessions.begin() as db:
        dataset = db.get(MarketDataset, dataset_id)
        metadata_payload: dict[str, object] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        if dataset is None:
            db.add(
                MarketDataset(
                    id=dataset_id,
                    source="octobot",
                    external_id=external_dataset_id,
                    symbol="BTC/USDT",
                    selected=True,
                    metadata_json=metadata_payload,
                    updated_at=now,
                )
            )
        else:
            dataset.selected = True
            dataset.metadata_json = metadata_payload
            dataset.updated_at = now

    snapshot = SnapshotResolver(registry.engine).resolve(
        {
            "costs": config.costs.model_dump(mode="json"),
            "risk": config.risk.model_dump(mode="json"),
            "strategy": config.strategy.model_dump(mode="json"),
        },
        code_version=code_version,
    )
    return {
        "dataset_id": dataset_id,
        "external_dataset_id": external_dataset_id,
        "configuration_version": snapshot.configuration_fingerprint,
    }
