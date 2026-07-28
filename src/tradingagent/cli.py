"""CLI boundary sharing the same application services as the HTTP API."""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import uvicorn
import yaml

from tradingagent.api.services import ApplicationRegistry
from tradingagent.config import AppConfig
from tradingagent.persistence.runtime import DurableJobWorker, JobScheduler

REGISTRY = (
    ApplicationRegistry.from_database_url(os.environ["DATABASE_URL"])
    if "DATABASE_URL" in os.environ
    else ApplicationRegistry()
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradingagent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve-api", help="run the internal FastAPI service")
    commands.add_parser("run-worker", help="run the isolated background worker")
    commands.add_parser("run-scheduler", help="run the isolated scheduler")

    config = commands.add_parser("config", help="configuration operations").add_subparsers(
        dest="action", required=True
    )
    validate = config.add_parser("validate")
    validate.add_argument("path", type=Path)

    data = commands.add_parser("data", help="market-data operations").add_subparsers(
        dest="action", required=True
    )
    data.add_parser("datasets")
    sync = data.add_parser("sync")
    sync.add_argument("--dataset-id", required=True)
    gaps = data.add_parser("gaps")
    gaps.add_argument("--dataset-id", required=True)

    backtest = commands.add_parser("backtest", help="backtest operations").add_subparsers(
        dest="action", required=True
    )
    run = backtest.add_parser("run")
    run.add_argument("--dataset-id", required=True)
    run.add_argument("--configuration-version", required=True)
    report = backtest.add_parser("report")
    report.add_argument("id")

    paper = commands.add_parser("paper", help="paper-session operations").add_subparsers(
        dest="action", required=True
    )
    create = paper.add_parser("create")
    create.add_argument("--dataset-id", required=True)
    create.add_argument("--configuration-version", required=True)
    for action in ("start", "pause", "resume", "stop", "status"):
        operation = paper.add_parser(action)
        operation.add_argument("id")

    database = commands.add_parser("db", help="database operations").add_subparsers(
        dest="action", required=True
    )
    database.add_parser("migrate")
    return parser


def _print(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    print(json.dumps(value, sort_keys=True, default=str))


def main(argv: list[str] | None = None) -> int:
    """Dispatch a command and return a process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "serve-api":
        uvicorn.run("tradingagent.api.app:app", host="0.0.0.0", port=8000)
        return 0
    if args.command == "run-worker":
        worker = DurableJobWorker(REGISTRY, {})
        while True:
            if not worker.run_once():
                time.sleep(1)
    if args.command == "run-scheduler":
        scheduler = JobScheduler(REGISTRY, interval=timedelta(minutes=15))
        while True:
            scheduler.enqueue_due()
            scheduler.enqueue_data_syncs()
            time.sleep(30)
    if args.command == "config":
        payload = yaml.safe_load(args.path.read_text())
        AppConfig.model_validate(payload)
        _print({"valid": True, "path": str(args.path)})
        return 0
    if args.command == "data":
        if args.action == "datasets":
            _print({"datasets": REGISTRY.datasets})
        elif args.action == "sync":
            _print(REGISTRY.create_job("data_sync"))
        else:
            _print({"dataset_id": args.dataset_id, "gaps": REGISTRY.gaps(args.dataset_id)})
        return 0
    if args.command == "backtest":
        if args.action == "run":
            _print(
                REGISTRY.create(
                    "backtest",
                    {
                        "dataset_id": args.dataset_id,
                        "configuration_version": args.configuration_version,
                    },
                )
            )
        else:
            _print(REGISTRY.backtest_report(args.id))
        return 0
    if args.command == "paper":
        if args.action == "create":
            _print(
                REGISTRY.create(
                    "paper",
                    {
                        "dataset_id": args.dataset_id,
                        "configuration_version": args.configuration_version,
                    },
                )
            )
        elif args.action == "status":
            _print(REGISTRY.resource(args.id, "paper_session"))
        else:
            _print(REGISTRY.transition(args.id, args.action))
        return 0
    if args.command == "db":
        return subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=False,
        ).returncode
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
