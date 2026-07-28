"""Exercise the deployed API, worker, history proxy and PostgreSQL end to end."""

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from urllib.request import Request, urlopen

import yaml
from sqlalchemy import func, select

from tradingagent.api.services import ApplicationRegistry
from tradingagent.config import database_url_from_environment
from tradingagent.persistence.models import (
    BacktestRun,
    CandleRecord,
    JobRecord,
    MarketDataset,
    PaperSession,
)
from tradingagent.persistence.snapshots import SnapshotResolver

URL = database_url_from_environment()
assert URL is not None
registry = ApplicationRegistry.from_database_url(URL)


def post(path: str, payload: dict[str, object], key: str) -> dict[str, object]:
    request = Request(
        f"http://api:8000{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Idempotency-Key": key},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310
        assert response.status in (200, 202)
        return json.load(response)


def wait_job(job_id: str) -> None:
    for _ in range(120):
        with registry.sessions() as db:
            job = db.get(JobRecord, job_id)
            assert job is not None
            if job.status == "completed":
                return
            if job.status == "failed":
                raise AssertionError((job.error_class, job.error_message))
        time.sleep(1)
    raise AssertionError(f"job did not complete: {job_id}")


def exercise() -> None:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start, end = now - timedelta(days=3), now - timedelta(days=1)
    with registry.sessions.begin() as db:
        db.add(
            MarketDataset(
                id="ci-dataset",
                source="octobot",
                external_id="ci-btc.data",
                symbol="BTC/USDT",
                selected=True,
                metadata_json={"start": start.isoformat(), "end": end.isoformat()},
                updated_at=now,
            )
        )
    config = yaml.safe_load(open("/app/config/config.yaml", encoding="utf-8"))  # noqa: PTH123
    resolved = SnapshotResolver(registry.engine).resolve(
        {key: config[key] for key in ("costs", "risk", "strategy")},
        code_version="compose-ci",
    )
    sync = post("/api/v1/data/sync", {"dataset_id": "ci-dataset"}, "ci-data-sync")
    wait_job(str(sync["id"]))
    with registry.sessions() as db:
        assert db.scalar(select(func.count()).select_from(CandleRecord)) > 10

    backtest = post(
        "/api/v1/backtests",
        {
            "dataset_id": "ci-dataset",
            "configuration_version": resolved.configuration_fingerprint,
        },
        "ci-backtest",
    )
    wait_job(str(backtest["job_id"]))
    with registry.sessions() as db:
        assert db.get(BacktestRun, str(backtest["resource_id"])).state == "completed"

    paper = post(
        "/api/v1/paper-sessions",
        {
            "dataset_id": "ci-dataset",
            "configuration_version": resolved.configuration_fingerprint,
        },
        "ci-paper",
    )
    paper_id = str(paper["resource_id"])
    wait_job(str(paper["job_id"]))
    started = post(f"/api/v1/paper-sessions/{paper_id}/start", {}, "ci-paper-start")
    assert started["state"] == "running"
    for cycle in range(2):
        job = registry.create_job(
            "paper_cycle",
            {"session_id": paper_id},
            idempotency_key=f"ci-paper-cycle-{cycle}",
        )
        wait_job(job.id)


def verify() -> None:
    with registry.sessions() as db:
        assert db.scalar(select(func.count()).select_from(CandleRecord)) > 10
        backtest = db.scalar(select(BacktestRun))
        paper = db.scalar(select(PaperSession))
        assert backtest is not None and backtest.state == "completed"
        assert paper is not None and paper.state == "running"
        completed_cycles = db.scalar(
            select(func.count())
            .select_from(JobRecord)
            .where(JobRecord.kind == "paper_cycle", JobRecord.status == "completed")
        )
        assert completed_cycles == 2


{"exercise": exercise, "verify": verify}[sys.argv[1]]()
