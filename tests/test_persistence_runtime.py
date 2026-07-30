from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from tradingagent.api.services import ApplicationRegistry
from tradingagent.persistence.models import Base, MarketDataset
from tradingagent.persistence.runtime import DurableJobWorker, JobScheduler


def engine():
    database = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(database)
    return database


def test_schema_contains_complete_domain_contract_and_query_indexes() -> None:
    database = engine()
    inspector = inspect(database)
    assert {
        "market_datasets",
        "candles",
        "candle_revisions",
        "data_gaps",
        "strategy_definitions",
        "feature_values",
        "signal_decisions",
        "backtest_runs",
        "backtest_events",
        "backtest_metrics",
        "paper_sessions",
        "orders",
        "fills",
        "positions",
        "cash_ledger",
        "equity_snapshots",
        "jobs",
        "audit_events",
        "idempotency_records",
    } <= set(inspector.get_table_names())
    candle_indexes = {index["name"] for index in inspector.get_indexes("candles")}
    assert "ix_candles_lookup" in candle_indexes
    assert "uq_candles_identity" in {
        constraint["name"] for constraint in inspector.get_unique_constraints("candles")
    }


def test_registry_resources_and_idempotency_survive_process_restart() -> None:
    database = engine()
    first = ApplicationRegistry(engine=database)
    operation = first.idempotent(
        "same-request",
        "backtest_create",
        {"dataset_id": "btc", "configuration_version": "v1"},
        lambda: first.create("backtest", {"dataset_id": "btc", "configuration_version": "v1"}),
    )

    restarted = ApplicationRegistry(engine=database)
    replay = restarted.idempotent(
        "same-request",
        "backtest_create",
        {"dataset_id": "btc", "configuration_version": "v1"},
        lambda: restarted.create("backtest", {"dataset_id": "wrong"}),
    )

    assert replay == operation
    assert restarted.resource(operation.resource_id, "backtest").request["dataset_id"] == "btc"
    assert restarted.job(operation.job_id).status == "queued"


def test_idempotency_failure_rolls_back_resource_and_job_atomically() -> None:
    database = engine()
    registry = ApplicationRegistry(engine=database)

    def create_then_crash():
        registry.create("backtest", {"dataset_id": "btc"})
        raise RuntimeError("crash before idempotency commit")

    try:
        registry.idempotent("crash", "backtest_create", {"dataset_id": "btc"}, create_then_crash)
    except RuntimeError:
        pass

    with registry.sessions() as session:
        from tradingagent.persistence.models import BacktestRun, IdempotencyRecord, JobRecord

        assert session.query(BacktestRun).count() == 0
        assert session.query(JobRecord).count() == 0
        assert session.query(IdempotencyRecord).count() == 0


def test_resource_transition_and_idempotency_are_one_transaction() -> None:
    database = engine()
    registry = ApplicationRegistry(engine=database)
    operation = registry.create("paper", {"dataset_id": "btc"})
    started = registry.idempotent(
        "start-once",
        f"paper_{operation.resource_id}_start",
        {},
        lambda: registry.transition(operation.resource_id, "start"),
    )
    restarted = ApplicationRegistry(engine=database)
    replay = restarted.idempotent(
        "start-once",
        f"paper_{operation.resource_id}_start",
        {},
        lambda: restarted.transition(operation.resource_id, "start"),
    )
    assert replay == started
    assert restarted.resource(operation.resource_id, "paper_session").state == "running"


def test_worker_claims_durable_jobs_records_progress_and_bounds_retries() -> None:
    database = engine()
    registry = ApplicationRegistry(engine=database)
    job = registry.create_job("data_sync")
    attempts = 0

    def handler(_payload, progress):
        nonlocal attempts
        attempts += 1
        progress(50)
        if attempts == 1:
            raise ConnectionError("temporary")
        progress(100)
        return {"candles": 4}

    worker = DurableJobWorker(registry, {"data_sync": handler}, maximum_retries=2)
    assert worker.run_once() is True
    completed = registry.job(job.id)
    assert completed.status == "completed"
    assert completed.progress == 100
    assert completed.retry_count == 1
    assert attempts == 2
    assert DurableJobWorker(registry, {"data_sync": handler}).run_once() is False


def test_scheduler_enqueues_each_due_cycle_once_across_restarts() -> None:
    database = engine()
    registry = ApplicationRegistry(engine=database)
    paper = registry.create("paper", {"dataset_id": "btc"})
    registry.transition(paper.resource_id, "start")
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    assert JobScheduler(registry, interval=timedelta(minutes=15)).enqueue_due(now) == 1
    assert (
        JobScheduler(
            ApplicationRegistry(engine=database), interval=timedelta(minutes=15)
        ).enqueue_due(now)
        == 0
    )


def test_scheduler_enqueues_selected_dataset_sync_once_per_interval() -> None:
    database = engine()
    registry = ApplicationRegistry(engine=database)
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    with registry.sessions.begin() as session:
        session.add(
            MarketDataset(
                id="btc",
                source="octobot",
                external_id="history.data",
                symbol="BTC/USDT",
                selected=True,
                metadata_json={},
                updated_at=now,
            )
        )

    scheduler = JobScheduler(registry, interval=timedelta(minutes=15))
    assert scheduler.enqueue_data_syncs(now) == 1
    assert scheduler.enqueue_data_syncs(now) == 0


def test_readiness_checks_database_schema_revision_and_runtime_configuration() -> None:
    database = engine()
    registry = ApplicationRegistry(engine=database, configuration_ready=True)
    status, dependencies = registry.readiness()
    assert status == "ok"
    assert dependencies["database"] == "available"
    assert dependencies["migrations"] == "current"

    Base.metadata.drop_all(database)
    status, dependencies = registry.readiness()
    assert status == "not_ready"
    assert dependencies["database"] == "unavailable"
