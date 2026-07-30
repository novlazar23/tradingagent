"""Validated immutable configuration and strategy lineage resolution."""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from tradingagent.config import StrategyValidationConfig
from tradingagent.persistence.models import ConfigurationSnapshot, StrategyDefinition


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True, slots=True)
class ResolvedSnapshot:
    """Stable references and fingerprints attached to every run."""

    snapshot_id: str
    strategy_definition_id: str
    configuration_fingerprint: str
    code_fingerprint: str
    configuration: StrategyValidationConfig


class SnapshotResolver:
    """Validate once, persist canonical immutable definitions and resolve by hash."""

    def __init__(self, engine: Engine) -> None:
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    def resolve(self, payload: dict[str, object], *, code_version: str) -> ResolvedSnapshot:
        if not code_version:
            raise ValueError("code_version is required")
        try:
            validated = StrategyValidationConfig.model_validate(payload)
        except ValidationError as exc:
            raise ValueError("invalid configuration snapshot") from exc
        canonical_payload = validated.model_dump(mode="json")
        canonical = _canonical(canonical_payload)
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        code_fingerprint = hashlib.sha256(code_version.encode()).hexdigest()
        snapshot_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"configuration:{fingerprint}"))
        strategy_json = validated.strategy.model_dump(mode="json")
        strategy_hash = hashlib.sha256(_canonical(strategy_json).encode()).hexdigest()
        strategy_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"strategy:{strategy_hash}"))
        now = datetime.now(UTC)
        with self.sessions.begin() as db:
            snapshot = db.scalar(
                select(ConfigurationSnapshot).where(
                    ConfigurationSnapshot.version_hash == fingerprint
                )
            )
            if snapshot is None:
                db.add(
                    ConfigurationSnapshot(
                        id=snapshot_id,
                        version_hash=fingerprint,
                        payload=canonical_payload,
                        created_at=now,
                    )
                )
            definition = db.scalar(
                select(StrategyDefinition).where(
                    StrategyDefinition.configuration_hash == strategy_hash
                )
            )
            if definition is None:
                db.add(
                    StrategyDefinition(
                        id=strategy_id,
                        version=f"{validated.strategy.version}:{strategy_hash[:12]}",
                        configuration=strategy_json,
                        configuration_hash=strategy_hash,
                        created_at=now,
                    )
                )
        return ResolvedSnapshot(
            snapshot_id,
            strategy_id,
            fingerprint,
            code_fingerprint,
            validated,
        )

    def get(self, version_hash: str, *, code_version: str) -> ResolvedSnapshot:
        with self.sessions() as db:
            snapshot = db.scalar(
                select(ConfigurationSnapshot).where(
                    ConfigurationSnapshot.version_hash == version_hash
                )
            )
            if snapshot is None:
                raise KeyError("unknown configuration snapshot")
        return self.resolve(snapshot.payload, code_version=code_version)
