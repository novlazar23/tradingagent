from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from tradingagent.api.services import ApplicationRegistry
from tradingagent.config import CostConfig, RiskConfig
from tradingagent.domain.models import Portfolio
from tradingagent.paper.engine import PaperEngine
from tradingagent.paper.repository import SQLAlchemyPaperRepository
from tradingagent.persistence.models import Base, JobRecord
from tradingagent.persistence.runtime import DurableJobWorker, RetryableJobError
from tradingagent.persistence.snapshots import SnapshotResolver
from tradingagent.trading.execution import ExecutionModel
from tradingagent.trading.risk import RiskEngine, SessionRiskState
from tradingagent.trading.strategy import StrategyEngine

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def database():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def paper_engine(repository: SQLAlchemyPaperRepository) -> PaperEngine:
    costs = CostConfig(
        maker_fee_rate=Decimal("0"),
        taker_fee_rate=Decimal("0.001"),
        spread_bps=Decimal("0"),
        slippage_model="fixed_bps",
        slippage_bps=Decimal("0"),
        atr_slippage_multiplier=None,
        price_quantum=Decimal("0.01"),
        quantity_quantum=Decimal("0.0001"),
        minimum_order_value=Decimal("10"),
        rounding_mode="ROUND_DOWN",
    )
    risks = RiskConfig(
        initial_capital=Decimal("10000"),
        maximum_position_fraction=Decimal("0.5"),
        risk_per_trade_fraction=Decimal("0.01"),
        stop_mode="percent",
        stop_distance=Decimal("0.02"),
        take_profit_distance=Decimal("0.04"),
        maximum_daily_loss_fraction=Decimal("0.03"),
        maximum_session_drawdown_fraction=Decimal("0.10"),
        maximum_entries_per_utc_day=3,
        cooldown_seconds=0,
        minimum_cash_reserve_fraction=Decimal("0.10"),
    )
    return PaperEngine(
        repository=repository,
        strategy=StrategyEngine(),
        risk=RiskEngine(risks),
        execution=ExecutionModel(costs),
        maximum_candle_age=timedelta(minutes=20),
    )


def test_sqlalchemy_paper_repository_restores_full_lifecycle_state_after_restart() -> None:
    engine = database()
    first = SQLAlchemyPaperRepository(engine)
    first.create_session(
        session_id="paper-1",
        portfolio=Portfolio(Decimal("10000"), Decimal("0")),
        risk_state=SessionRiskState.initial(Decimal("10000")),
        configuration_versions=("strategy-v1", "cost-v1", "risk-v1"),
    )
    started = paper_engine(first).start("paper-1", now=NOW)
    paused = paper_engine(first).pause(
        "paper-1", actor="operator", reason="maintenance", now=NOW + timedelta(seconds=1)
    )

    restored = SQLAlchemyPaperRepository(engine).get("paper-1")

    assert started.status.value == "RUNNING"
    assert restored == paused
    assert restored.audit_events[-1].details == "maintenance"
    assert restored.configuration_versions == ("strategy-v1", "cost-v1", "risk-v1")


def test_registry_lifecycle_transition_updates_complete_persisted_paper_state() -> None:
    engine = database()
    repository = SQLAlchemyPaperRepository(engine)
    repository.create_session(
        session_id="paper-api",
        portfolio=Portfolio(Decimal("10000"), Decimal("0")),
        risk_state=SessionRiskState.initial(Decimal("10000")),
        configuration_versions=("strategy-v1", "cost-v1", "risk-v1"),
    )
    registry = ApplicationRegistry(engine=engine)

    view = registry.transition("paper-api", "start")
    restored = SQLAlchemyPaperRepository(engine).get("paper-api")

    assert view.state == "running"
    assert restored.status.value == "RUNNING"
    assert restored.audit_events[-1].event_type == "start"


def test_expired_job_lease_is_reclaimed_after_worker_crash() -> None:
    engine = database()
    registry = ApplicationRegistry(engine=engine)
    job = registry.create_job("recover")
    with registry.sessions.begin() as session:
        row = session.get(JobRecord, job.id)
        assert row is not None
        row.status = "running"
        row.lease_owner = "dead-worker"
        row.lease_expires_at = NOW - timedelta(seconds=1)

    worker = DurableJobWorker(
        registry,
        {"recover": lambda _payload, _progress: {"recovered": True}},
        worker_id="replacement",
        lease_duration=timedelta(minutes=1),
    )
    assert worker.run_once(now=NOW) is True
    assert registry.job(job.id).status == "completed"


def test_retryable_job_is_requeued_with_persisted_backoff_instead_of_busy_retry() -> None:
    engine = database()
    registry = ApplicationRegistry(engine=engine)
    job = registry.create_job("sync")

    def transient(_payload, _progress):
        raise RetryableJobError("upstream unavailable")

    worker = DurableJobWorker(
        registry,
        {"sync": transient},
        worker_id="worker-1",
        retry_jitter=lambda _job_id, _attempt: 0,
    )
    assert worker.run_once(now=NOW) is True
    with registry.sessions() as session:
        row = session.scalar(select(JobRecord).where(JobRecord.id == job.id))
        assert row is not None
        assert row.status == "queued"
        assert row.retry_count == 1
        assert row.available_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=2)
        assert row.error_message == "The operation will be retried"


def test_configuration_snapshot_is_validated_canonical_and_immutable() -> None:
    engine = database()
    resolver = SnapshotResolver(engine)
    payload = {
        "strategy": {
            "version": "strategy-v1",
            "timeframe_weights": {"15m": "1", "1h": "1", "4h": "1", "1d": "1"},
            "indicator_parameters": {},
            "pattern_parameters": {},
            "entry_threshold": "0.5",
            "exit_threshold": "-0.5",
            "minimum_confidence": "0.5",
            "minimum_confirming_groups": 1,
            "cooldown_seconds": 0,
            "higher_timeframe_mode": "weighted",
        },
        "risk": {
            "initial_capital": "10000",
            "maximum_position_fraction": "0.5",
            "risk_per_trade_fraction": "0.01",
            "stop_mode": "percent",
            "stop_distance": "0.02",
            "take_profit_distance": "0.04",
            "maximum_daily_loss_fraction": "0.03",
            "maximum_session_drawdown_fraction": "0.1",
            "maximum_entries_per_utc_day": 3,
            "cooldown_seconds": 0,
            "minimum_cash_reserve_fraction": "0.1",
        },
        "costs": {
            "maker_fee_rate": "0",
            "taker_fee_rate": "0.001",
            "spread_bps": "1",
            "slippage_model": "fixed_bps",
            "slippage_bps": "1",
            "atr_slippage_multiplier": None,
            "price_quantum": "0.01",
            "quantity_quantum": "0.0001",
            "minimum_order_value": "10",
            "rounding_mode": "ROUND_DOWN",
        },
    }
    first = resolver.resolve(payload, code_version="git:abc")
    second = resolver.resolve(dict(reversed(list(payload.items()))), code_version="git:abc")

    assert first.snapshot_id == second.snapshot_id
    assert first.configuration_fingerprint == second.configuration_fingerprint
    assert first.code_fingerprint != first.configuration_fingerprint
