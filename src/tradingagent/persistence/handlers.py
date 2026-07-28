"""Concrete durable job handlers composing data, backtest and paper engines."""

import json
import uuid
from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradingagent.api.services import ApplicationRegistry
from tradingagent.backtest import BacktestConfig, BacktestEngine
from tradingagent.config import AppConfig, StrategyConfig
from tradingagent.domain.models import Candle, Portfolio
from tradingagent.features import (
    IndicatorConfig,
    PatternConfig,
    PatternEngine,
    calculate_indicators,
)
from tradingagent.market_data.adapter import OctoBotHistoryClient
from tradingagent.market_data.timeframes import parse_timeframe
from tradingagent.paper import CandleHealth, PaperCycle, PaperEngine
from tradingagent.paper.repository import SQLAlchemyPaperRepository
from tradingagent.persistence.models import (
    BacktestEvent,
    BacktestMetric,
    BacktestRun,
    CandleRecord,
    CandleRevision,
    DataGap,
    MarketDataset,
)
from tradingagent.persistence.snapshots import ResolvedSnapshot, SnapshotResolver
from tradingagent.trading.execution import ExecutionModel
from tradingagent.trading.pipeline import TradingPipeline
from tradingagent.trading.risk import RiskEngine, SessionRiskState
from tradingagent.trading.strategy import (
    SignalContribution,
    StrategyEngine,
    StrategyRequest,
)

_FEATURE_LOOKBACK_CANDLES = 500
_TIMEFRAME_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def _json(value: object) -> object:
    return json.loads(json.dumps(value, default=str))


def _fence(progress: Callable[[int], None], session: Session) -> None:
    """Fence a production job transaction when progress carries a claim token."""
    fence = getattr(progress, "fence", None)
    if callable(fence):
        fence(session)


class RuntimeHandlerFactory:
    """Build production handlers with explicit dependencies and immutable lineage."""

    def __init__(
        self,
        registry: ApplicationRegistry,
        *,
        config: AppConfig,
        history: OctoBotHistoryClient,
        code_version: str,
    ) -> None:
        self.registry = registry
        self.config = config
        self.history = history
        self.code_version = code_version
        self.snapshots = SnapshotResolver(registry.engine)
        self.paper_repository = SQLAlchemyPaperRepository(registry.engine)

    def handlers(self) -> dict[str, object]:
        return {
            "data_sync": self.data_sync,
            "backtest_create": self.backtest_create,
            "paper_create": self.paper_create,
            "paper_cycle": self.paper_cycle,
        }

    def data_sync(
        self, payload: dict[str, object], progress: Callable[[int], None]
    ) -> dict[str, object]:
        dataset_id = str(payload["dataset_id"])
        now = datetime.now(UTC)
        with self.registry.sessions() as db:
            dataset = db.get(MarketDataset, dataset_id)
            if dataset is None:
                raise ValueError("unknown dataset")
            metadata = dataset.metadata_json
            external_id = dataset.external_id
        start = datetime.fromisoformat(str(payload.get("start") or metadata["start"]))
        end = datetime.fromisoformat(str(payload.get("end") or metadata["end"]))
        requested_execution_candles = int((end - start).total_seconds() // (15 * 60))
        if requested_execution_candles > self.config.deployment.backtest_max_candles:
            raise ValueError(
                "data sync range exceeds configured backtest_max_candles="
                f"{self.config.deployment.backtest_max_candles}"
            )
        inserted = revised = 0
        for timeframe_value in ("15m", "1h", "4h", "1d"):
            timeframe = parse_timeframe(timeframe_value)
            candles = tuple(
                self.history.iter_candles(
                    dataset_id=external_id,
                    symbol="BTC/USDT",
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    limit=self.config.deployment.history_page_limit,
                    now=now,
                )
            )
            with self.registry.sessions.begin() as db:
                _fence(progress, db)
                for candle in candles:
                    existing = db.scalar(
                        select(CandleRecord).where(
                            CandleRecord.source == candle.source,
                            CandleRecord.dataset_id == dataset_id,
                            CandleRecord.symbol == candle.symbol,
                            CandleRecord.timeframe == candle.timeframe,
                            CandleRecord.open_time == candle.open_time,
                        )
                    )
                    values = _candle_values(candle, dataset_id)
                    if existing is None:
                        db.add(CandleRecord(id=str(uuid.uuid4()), **values))
                        inserted += 1
                    elif existing.source_fingerprint != candle.source_fingerprint:
                        before = _record_payload(existing)
                        for name, value in values.items():
                            setattr(existing, name, value)
                        db.add(
                            CandleRevision(
                                id=str(uuid.uuid4()),
                                candle_id=existing.id,
                                before=before,
                                after=_json(values),
                                revised_at=now,
                            )
                        )
                        revised += 1
                present = {candle.open_time for candle in candles}
                cursor = start
                step = timedelta(
                    seconds={"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[timeframe]
                )
                while cursor < end:
                    if cursor not in present:
                        existing_gap = db.scalar(
                            select(DataGap.id).where(
                                DataGap.dataset_id == dataset_id,
                                DataGap.timeframe == timeframe,
                                DataGap.start_time == cursor,
                            )
                        )
                        if existing_gap is None:
                            db.add(
                                DataGap(
                                    id=str(uuid.uuid4()),
                                    dataset_id=dataset_id,
                                    timeframe=timeframe,
                                    start_time=cursor,
                                    end_time=cursor + step,
                                    reason="missing_source_candle",
                                    detected_at=now,
                                )
                            )
                    cursor += step
            progress(int((("15m", "1h", "4h", "1d").index(timeframe) + 1) * 25))
        return {"inserted": inserted, "revised": revised}

    def backtest_create(
        self, payload: dict[str, object], progress: Callable[[int], None]
    ) -> dict[str, object]:
        run_id = str(payload["resource_id"])
        fingerprint = str(payload["configuration_version"])
        resolved = self.snapshots.get(fingerprint, code_version=self.code_version)
        dataset_id = str(payload["dataset_id"])
        with self.registry.sessions.begin() as db:
            _fence(progress, db)
            run = db.get(BacktestRun, run_id)
            if run is None:
                raise ValueError("unknown backtest")
            run.state = "running"
            rows: list[CandleRecord] = []
            for timeframe in ("15m", "1h", "4h", "1d"):
                maximum = (
                    self.config.deployment.backtest_max_candles
                    * 15
                    // _TIMEFRAME_MINUTES[timeframe]
                )
                timeframe_rows = db.scalars(
                    select(CandleRecord)
                    .where(
                        CandleRecord.dataset_id == dataset_id,
                        CandleRecord.timeframe == timeframe,
                        CandleRecord.is_closed.is_(True),
                    )
                    .order_by(CandleRecord.close_time)
                    .limit(maximum + 1)
                ).all()
                if len(timeframe_rows) > maximum:
                    raise ValueError(
                        f"backtest exceeds configured candle bound for {timeframe}: {maximum}"
                    )
                rows.extend(timeframe_rows)
            gaps = db.scalars(select(DataGap).where(DataGap.dataset_id == dataset_id)).all()
        candles_by_timeframe = _rows_by_timeframe(rows)
        candle_ids = _candle_ids(rows)
        candles = candles_by_timeframe["15m"]
        source_data_fingerprint = _data_fingerprint(rows)
        if len(candles) < 2:
            raise ValueError("backtest requires at least two persisted 15m candles")
        progress(20)
        execution = ExecutionModel(resolved.configuration.costs)
        pipeline = TradingPipeline(
            StrategyEngine(),
            RiskEngine(resolved.configuration.risk),
            execution,
        )
        engine = BacktestEngine(
            execution_model=execution,
            config=BacktestConfig(
                initial_capital=resolved.configuration.risk.initial_capital,
                strategy_version=resolved.configuration.strategy.version,
                strategy_configuration_json=json.dumps(
                    resolved.configuration.strategy.model_dump(mode="json"), sort_keys=True
                ),
                maximum_candles=self.config.deployment.backtest_max_candles,
            ),
            pipeline=pipeline,
        )
        report = engine.run(
            candles,
            lambda history, position: _strategy_request(
                strategy=resolved.configuration.strategy,
                candles_by_timeframe=candles_by_timeframe,
                rows=rows,
                candle_ids=candle_ids,
                decision_time=history[-1].close_time,
                has_position=position,
                gaps=gaps,
            ),
        )
        progress(80)
        serialized = _json(asdict(report))
        with self.registry.sessions.begin() as db:
            _fence(progress, db)
            run = db.get(BacktestRun, run_id)
            assert run is not None
            run.state = "completed"
            run.result = {
                "report": serialized,
                "configuration_fingerprint": resolved.configuration_fingerprint,
                "code_fingerprint": resolved.code_fingerprint,
                "data_fingerprint": source_data_fingerprint,
                "execution_data_fingerprint": report.snapshot.data_fingerprint,
                "lineage": {
                    "dataset_id": dataset_id,
                    "strategy_definition_id": resolved.strategy_definition_id,
                    "configuration_snapshot_id": resolved.snapshot_id,
                    "configuration_fingerprint": resolved.configuration_fingerprint,
                    "code_fingerprint": resolved.code_fingerprint,
                    "data_fingerprint": source_data_fingerprint,
                    "execution_data_fingerprint": report.snapshot.data_fingerprint,
                },
            }
            run.updated_at = datetime.now(UTC)
            db.add(
                BacktestEvent(
                    id=str(uuid.uuid4()),
                    run_id=run_id,
                    event_type="run_completed",
                    event_time=run.updated_at,
                    payload={"snapshot": _json(asdict(report.snapshot))},
                )
            )
            for name, value in asdict(report.metrics).items():
                db.add(
                    BacktestMetric(
                        id=str(uuid.uuid4()),
                        run_id=run_id,
                        name=name,
                        value=value if isinstance(value, Decimal) else None,
                        payload=None if isinstance(value, Decimal) else {"value": value},
                    )
                )
        return {"backtest_id": run_id, "data_fingerprint": source_data_fingerprint}

    def paper_create(
        self, payload: dict[str, object], progress: Callable[[int], None]
    ) -> dict[str, object]:
        session_id = str(payload["resource_id"])
        resolved = self.snapshots.get(
            str(payload["configuration_version"]), code_version=self.code_version
        )
        repository = SQLAlchemyPaperRepository(
            self.registry.engine,
            transaction_guard=lambda db: _fence(progress, db),
        )
        repository.create_session(
            session_id=session_id,
            portfolio=Portfolio(resolved.configuration.risk.initial_capital, Decimal(0)),
            risk_state=SessionRiskState.initial(resolved.configuration.risk.initial_capital),
            configuration_versions=(
                resolved.strategy_definition_id,
                resolved.snapshot_id,
                resolved.code_fingerprint,
            ),
        )
        progress(100)
        return {"paper_session_id": session_id}

    def paper_cycle(
        self, payload: dict[str, object], progress: Callable[[int], None]
    ) -> dict[str, object]:
        session_id = str(payload["session_id"])
        repository = SQLAlchemyPaperRepository(
            self.registry.engine,
            transaction_guard=lambda db: _fence(progress, db),
        )
        state = repository.get(session_id)
        snapshot_id = state.configuration_versions[1]
        with self.registry.sessions() as db:
            snapshot = db.get(
                __import__(
                    "tradingagent.persistence.models", fromlist=["ConfigurationSnapshot"]
                ).ConfigurationSnapshot,
                snapshot_id,
            )
            paper = db.get(
                __import__(
                    "tradingagent.persistence.models", fromlist=["PaperSession"]
                ).PaperSession,
                session_id,
            )
            if snapshot is None or paper is None:
                raise ValueError("paper lineage is incomplete")
            dataset_id = str(paper.request.get("dataset_id", payload.get("dataset_id", "")))
            rows = db.scalars(
                select(CandleRecord)
                .where(
                    CandleRecord.dataset_id == dataset_id,
                    CandleRecord.is_closed.is_(True),
                )
                .order_by(CandleRecord.close_time)
            ).all()
            gaps = db.scalars(select(DataGap).where(DataGap.dataset_id == dataset_id)).all()
        resolved = self.snapshots.resolve(snapshot.payload, code_version=self.code_version)
        engine = self._paper_engine(resolved, repository=repository)
        checkpoint_time = state.checkpoint.candle_close_time if state.checkpoint else None
        execution_rows = _paper_execution_rows(rows, checkpoint_time)
        if not execution_rows:
            progress(100)
            return {
                "paper_session_id": session_id,
                "state": state.status.value.lower(),
                "processed_candles": 0,
            }
        result = state
        for index, candle in enumerate(execution_rows, start=1):
            decision_time = _aware(candle.close_time)
            visible_rows = [row for row in rows if _aware(row.close_time) <= decision_time]
            candles_by_timeframe = _rows_by_timeframe(visible_rows)
            request = _strategy_request(
                strategy=resolved.configuration.strategy,
                candles_by_timeframe=candles_by_timeframe,
                rows=visible_rows,
                decision_time=decision_time,
                has_position=result.ledger.portfolio.btc > 0,
                gaps=gaps,
            )
            matching_gap = any(
                _aware(gap.start_time) < decision_time and _aware(gap.end_time) <= decision_time
                for gap in gaps
            )
            source_available = all(
                any(_aware(row.close_time) <= decision_time for row in candles_by_timeframe[tf])
                for tf in ("15m", "1h", "4h", "1d")
            )
            portfolio = result.ledger.portfolio
            result = engine.process(
                session_id,
                PaperCycle(
                    candle_id=candle.id,
                    candle_close_time=decision_time,
                    # A catch-up cycle recreates the decision at candle close. Using
                    # wall time would incorrectly reject all downtime candles as stale.
                    observed_at=decision_time,
                    candle_open=candle.open,
                    reference_price=candle.close,
                    candle_low=candle.low,
                    candle_high=candle.high,
                    atr=_latest_atr(
                        candles_by_timeframe["15m"],
                        resolved.configuration.strategy.indicator_parameters,
                    ),
                    strategy_request=request,
                    health=CandleHealth(
                        source_available,
                        matching_gap,
                        False,
                        portfolio.cash >= 0 and portfolio.btc >= 0,
                    ),
                ),
            )
            progress(index * 100 // len(execution_rows))
        return {
            "paper_session_id": session_id,
            "state": result.status.value.lower(),
            "processed_candles": len(execution_rows),
        }

    def _paper_engine(
        self,
        resolved: ResolvedSnapshot,
        *,
        repository: SQLAlchemyPaperRepository | None = None,
    ) -> PaperEngine:
        configuration = resolved.configuration
        return PaperEngine(
            repository=repository or self.paper_repository,
            strategy=StrategyEngine(),
            risk=RiskEngine(configuration.risk),
            execution=ExecutionModel(configuration.costs),
            maximum_candle_age=timedelta(
                seconds=self.config.deployment.paper_max_candle_age_seconds
            ),
        )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


_INDICATOR_DEFAULTS: dict[str, Decimal] = {
    "sma_period": Decimal(20),
    "ema_period": Decimal(20),
    "rsi_period": Decimal(14),
    "macd_fast_period": Decimal(12),
    "macd_slow_period": Decimal(26),
    "macd_signal_period": Decimal(9),
    "bollinger_period": Decimal(20),
    "bollinger_stddev": Decimal(2),
    "atr_period": Decimal(14),
    "volume_period": Decimal(20),
    "rsi_oversold": Decimal(30),
    "rsi_overbought": Decimal(70),
}
_PATTERN_DEFAULTS: dict[str, Decimal] = {
    "pivot_window": Decimal(2),
    "similar_level_tolerance": Decimal("0.03"),
    "minimum_separation": Decimal(2),
    "breakout_tolerance": Decimal(0),
    "doji_body_ratio": Decimal("0.1"),
    "wick_body_ratio": Decimal(2),
    "volume_confirmation_ratio": Decimal(1),
}


def _indicator_config(parameters: Mapping[str, Decimal]) -> IndicatorConfig:
    values = {**_INDICATOR_DEFAULTS, **parameters}
    return IndicatorConfig(
        sma_period=int(values["sma_period"]),
        ema_period=int(values["ema_period"]),
        rsi_period=int(values["rsi_period"]),
        macd_fast_period=int(values["macd_fast_period"]),
        macd_slow_period=int(values["macd_slow_period"]),
        macd_signal_period=int(values["macd_signal_period"]),
        bollinger_period=int(values["bollinger_period"]),
        bollinger_stddev=values["bollinger_stddev"],
        atr_period=int(values["atr_period"]),
        volume_period=int(values["volume_period"]),
        rsi_oversold=values["rsi_oversold"],
        rsi_overbought=values["rsi_overbought"],
    )


def _pattern_config(parameters: Mapping[str, Decimal]) -> PatternConfig:
    values = {**_PATTERN_DEFAULTS, **parameters}
    return PatternConfig(
        pivot_window=int(values["pivot_window"]),
        similar_level_tolerance=values["similar_level_tolerance"],
        minimum_separation=int(values["minimum_separation"]),
        breakout_tolerance=values["breakout_tolerance"],
        doji_body_ratio=values["doji_body_ratio"],
        wick_body_ratio=values["wick_body_ratio"],
        volume_confirmation_ratio=values["volume_confirmation_ratio"],
    )


def _rows_by_timeframe(
    rows: Sequence[CandleRecord],
) -> dict[str, tuple[Candle, ...]]:
    return {
        timeframe: tuple(
            _domain_candle(row) for row in rows if row.timeframe == timeframe and row.is_closed
        )
        for timeframe in ("15m", "1h", "4h", "1d")
    }


def _paper_execution_rows(
    rows: Sequence[CandleRecord], checkpoint_close: datetime | None
) -> list[CandleRecord]:
    """Return closed 15m candles strictly after a durable checkpoint."""
    checkpoint = _aware(checkpoint_close) if checkpoint_close is not None else None
    return sorted(
        (
            row
            for row in rows
            if row.timeframe == "15m"
            and row.is_closed
            and (checkpoint is None or _aware(row.close_time) > checkpoint)
        ),
        key=lambda row: (_aware(row.close_time), row.id),
    )


def _strategy_request(
    *,
    strategy: StrategyConfig,
    candles_by_timeframe: Mapping[str, tuple[Candle, ...]],
    rows: Sequence[CandleRecord],
    candle_ids: Mapping[tuple[str, datetime], str] | None = None,
    decision_time: datetime,
    has_position: bool,
    gaps: Sequence[DataGap],
) -> StrategyRequest:
    # StrategyConfig is kept structural here to avoid coupling feature code to Pydantic.
    indicator_parameters = strategy.indicator_parameters
    pattern_parameters = strategy.pattern_parameters
    indicator_config = _indicator_config(indicator_parameters)
    pattern_engine = PatternEngine(_pattern_config(pattern_parameters))
    ids = candle_ids if candle_ids is not None else _candle_ids(rows)
    contributions: list[SignalContribution] = []
    blockers: list[str] = []
    indicator_groups = {
        "sma": "trend",
        "ema": "trend",
        "rsi": "momentum",
        "macd": "momentum",
        "bollinger": "volatility",
        "atr": "volatility",
        "volume": "volume",
    }
    for timeframe in ("15m", "1h", "4h", "1d"):
        timeframe_candles = candles_by_timeframe[timeframe]
        visible_end = bisect_right(
            timeframe_candles, decision_time, key=lambda candle: candle.close_time
        )
        visible = timeframe_candles[max(0, visible_end - _FEATURE_LOOKBACK_CANDLES) : visible_end]
        if not visible:
            blockers.append(f"missing closed {timeframe} candle")
            continue
        latest_id = ids.get((timeframe, visible[-1].close_time))
        if latest_id is None:
            blockers.append(f"missing lineage for {timeframe} candle")
            continue
        indicators = calculate_indicators(visible, indicator_config)
        if not indicators.tradeable:
            blockers.append(f"{timeframe} indicator warm-up incomplete")
        else:
            for name, evidence in indicators.contributions.items():
                contributions.append(
                    SignalContribution(
                        f"indicator:{name}",
                        indicators.version,
                        (latest_id,),
                        indicator_groups[name],
                        timeframe,
                        evidence.score,
                        Decimal(1),
                        evidence.reason,
                    )
                )
        for detection in pattern_engine.detect(visible, decision_time):
            if detection.available_at != visible[-1].close_time:
                continue
            score = {
                "bullish": Decimal(1),
                "bearish": Decimal(-1),
                "neutral": Decimal(0),
            }[detection.direction]
            contributions.append(
                SignalContribution(
                    f"pattern:{detection.name}",
                    detection.version,
                    (latest_id,),
                    "pattern",
                    timeframe,
                    score,
                    detection.confidence,
                    "; ".join(detection.evidence),
                )
            )
    for gap in gaps:
        if _aware(gap.start_time) < decision_time and _aware(gap.end_time) <= decision_time:
            blockers.append(f"data gap {gap.timeframe} {_aware(gap.start_time).isoformat()}")
    group_weights = {
        "trend": indicator_parameters.get("trend_weight", Decimal(1)),
        "momentum": indicator_parameters.get("momentum_weight", Decimal(1)),
        "volatility": indicator_parameters.get("volatility_weight", Decimal(1)),
        "volume": indicator_parameters.get("volume_weight", Decimal(1)),
        "pattern": pattern_parameters.get("pattern_weight", Decimal(1)),
    }
    return StrategyRequest(
        decision_time=decision_time,
        strategy_version=strategy.version,
        contributions=tuple(contributions),
        group_weights=group_weights,
        timeframe_weights=cast(Mapping[str, Decimal], strategy.timeframe_weights),
        entry_threshold=strategy.entry_threshold,
        exit_threshold=strategy.exit_threshold,
        minimum_confidence=strategy.minimum_confidence,
        minimum_confirming_groups=strategy.minimum_confirming_groups,
        has_position=has_position,
        data_blockers=tuple(sorted(set(blockers))),
    )


def _latest_atr(candles: tuple[Candle, ...], parameters: Mapping[str, Decimal]) -> Decimal | None:
    if not candles:
        return None
    return calculate_indicators(candles, _indicator_config(parameters)).atr


def _candle_ids(rows: Sequence[CandleRecord]) -> dict[tuple[str, datetime], str]:
    return {(row.timeframe, _aware(row.close_time)): row.id for row in rows if row.is_closed}


def _data_fingerprint(rows: Sequence[CandleRecord]) -> str:
    hasher = sha256()
    for row in sorted(rows, key=lambda item: (item.timeframe, _aware(item.open_time))):
        payload = (
            row.source,
            row.dataset_id,
            row.symbol,
            row.timeframe,
            _aware(row.open_time).isoformat(),
            row.source_fingerprint,
        )
        hasher.update(json.dumps(payload, separators=(",", ":")).encode())
        hasher.update(b"\n")
    return hasher.hexdigest()


def _candle_values(candle: Candle, dataset_id: str) -> dict[str, object]:
    return {
        "source": candle.source,
        "dataset_id": dataset_id,
        "symbol": candle.symbol,
        "timeframe": candle.timeframe,
        "open_time": candle.open_time,
        "close_time": candle.close_time,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "is_closed": candle.is_closed,
        "source_fingerprint": candle.source_fingerprint,
        "ingested_at": candle.ingested_at,
    }


def _record_payload(row: CandleRecord) -> dict[str, object]:
    return cast(
        dict[str, object],
        _json(
            {
                name: getattr(row, name)
                for name in _candle_values(_domain_candle(row), row.dataset_id)
            }
        ),
    )


def _domain_candle(row: CandleRecord) -> Candle:
    return Candle(
        row.source,
        row.dataset_id,
        cast(Literal["BTC/USDT"], row.symbol),
        cast(Literal["15m", "1h", "4h", "1d"], row.timeframe),
        _aware(row.open_time),
        _aware(row.close_time),
        row.open,
        row.high,
        row.low,
        row.close,
        row.volume,
        row.is_closed,
        row.source_fingerprint,
        _aware(row.ingested_at),
    )
