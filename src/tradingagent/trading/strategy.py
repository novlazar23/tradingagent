"""Explainable, deterministic signal aggregation and conflict resolution."""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Literal


class DecisionAction(StrEnum):
    """Action emitted once for each closed execution-timeframe candle."""

    ENTER_LONG = "ENTER_LONG"
    EXIT_LONG = "EXIT_LONG"
    HOLD = "HOLD"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class SignalContribution:
    """Versioned feature evidence contributing to a strategy decision."""

    feature_id: str
    feature_version: str
    candle_ids: tuple[str, ...]
    group: str
    timeframe: Literal["15m", "1h", "4h", "1d"]
    score: Decimal
    confidence: Decimal
    reason: str

    def __post_init__(self) -> None:
        if not self.feature_id or not self.feature_version or not self.candle_ids:
            raise ValueError("feature lineage must be complete")
        if not -1 <= self.score <= 1:
            raise ValueError("contribution score must be in [-1, 1]")
        if not 0 <= self.confidence <= 1:
            raise ValueError("contribution confidence must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class StrategyRequest:
    """Complete immutable input to one strategy decision."""

    decision_time: datetime
    strategy_version: str
    contributions: tuple[SignalContribution, ...]
    group_weights: Mapping[str, Decimal]
    timeframe_weights: Mapping[str, Decimal]
    entry_threshold: Decimal
    exit_threshold: Decimal
    minimum_confidence: Decimal
    minimum_confirming_groups: int
    has_position: bool
    data_blockers: tuple[str, ...] = ()
    forced_exit_reasons: tuple[str, ...] = ()
    cooldown_active: bool = False
    higher_timeframe_blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision_time.tzinfo is None or self.decision_time.utcoffset() != UTC.utcoffset(
            self.decision_time
        ):
            raise ValueError("decision_time must be timezone-aware UTC")
        if not self.strategy_version:
            raise ValueError("strategy_version is required")
        if self.minimum_confirming_groups <= 0:
            raise ValueError("minimum_confirming_groups must be positive")
        if not -1 <= self.entry_threshold <= 1 or not -1 <= self.exit_threshold <= 1:
            raise ValueError("thresholds must be in [-1, 1]")
        all_weights = (*self.group_weights.values(), *self.timeframe_weights.values())
        if any(weight < 0 for weight in all_weights):
            raise ValueError("weights cannot be negative")


@dataclass(frozen=True, slots=True)
class SignalDecision:
    """Auditable output retaining all versions, evidence and rejection reasons."""

    decision_id: str
    input_fingerprint: str
    decision_time: datetime
    strategy_version: str
    engine_version: str
    action: DecisionAction
    aggregate_score: Decimal
    aggregate_confidence: Decimal
    confirming_groups: tuple[str, ...]
    contributions: tuple[SignalContribution, ...]
    reasons: tuple[str, ...]


class StrategyEngine:
    """Aggregate weighted evidence and apply the specified safety priority."""

    def __init__(self, version: str = "strategy-engine-v1") -> None:
        if not version:
            raise ValueError("engine version is required")
        self.version = version

    def decide(self, request: StrategyRequest) -> SignalDecision:
        """Return one reproducibly identified decision for the complete input."""
        score, confidence, groups = self._aggregate(request)
        entry = (
            score >= request.entry_threshold
            and confidence >= request.minimum_confidence
            and len([group for group, value in groups.items() if value > 0])
            >= request.minimum_confirming_groups
        )
        exit_ = (
            score <= request.exit_threshold
            and confidence >= request.minimum_confidence
            and len([group for group, value in groups.items() if value < 0])
            >= request.minimum_confirming_groups
        )

        if request.data_blockers or request.higher_timeframe_blockers:
            action = DecisionAction.BLOCKED
            reasons = tuple((*request.data_blockers, *request.higher_timeframe_blockers))
        elif request.forced_exit_reasons and request.has_position:
            action = DecisionAction.EXIT_LONG
            reasons = request.forced_exit_reasons
        elif entry and exit_ and request.has_position:
            action = DecisionAction.EXIT_LONG
            reasons = ("exit priority over simultaneous entry",)
        elif request.cooldown_active:
            action = DecisionAction.BLOCKED
            reasons = ("cooldown active",)
        elif exit_ and request.has_position:
            action = DecisionAction.EXIT_LONG
            reasons = ("exit threshold reached",)
        elif entry and not request.has_position:
            action = DecisionAction.ENTER_LONG
            reasons = ("entry threshold reached",)
        elif entry and request.has_position:
            action = DecisionAction.HOLD
            reasons = ("existing long position prevents another entry",)
        else:
            action = DecisionAction.HOLD
            reasons = ("conflicting evidence or threshold not reached",)

        fingerprint = self._fingerprint(request)
        decision_id = sha256(f"{self.version}:{fingerprint}".encode()).hexdigest()
        return SignalDecision(
            decision_id=decision_id,
            input_fingerprint=fingerprint,
            decision_time=request.decision_time,
            strategy_version=request.strategy_version,
            engine_version=self.version,
            action=action,
            aggregate_score=score,
            aggregate_confidence=confidence,
            confirming_groups=tuple(sorted(group for group, value in groups.items() if value)),
            contributions=request.contributions,
            reasons=reasons,
        )

    @staticmethod
    def _aggregate(
        request: StrategyRequest,
    ) -> tuple[Decimal, Decimal, dict[str, Decimal]]:
        weighted: list[tuple[SignalContribution, Decimal]] = []
        for item in request.contributions:
            configured_group_weight = request.group_weights.get(item.group, Decimal(0))
            timeframe_weight = request.timeframe_weights.get(item.timeframe, Decimal(0))
            weight = configured_group_weight * timeframe_weight
            if weight > 0:
                weighted.append((item, weight))
        total_weight = sum((weight for _, weight in weighted), Decimal(0))
        if total_weight == 0:
            return Decimal(0), Decimal(0), {}
        score = sum((item.score * weight for item, weight in weighted), Decimal(0)) / total_weight
        confidence = (
            sum((item.confidence * weight for item, weight in weighted), Decimal(0)) / total_weight
        )
        groups: dict[str, Decimal] = {}
        group_weight: dict[str, Decimal] = {}
        for item, weight in weighted:
            groups[item.group] = groups.get(item.group, Decimal(0)) + item.score * weight
            group_weight[item.group] = group_weight.get(item.group, Decimal(0)) + weight
        return (
            score,
            confidence,
            {group: value / group_weight[group] for group, value in groups.items()},
        )

    def _fingerprint(self, request: StrategyRequest) -> str:
        payload = asdict(request)
        payload["engine_version"] = self.version
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return sha256(canonical.encode()).hexdigest()
