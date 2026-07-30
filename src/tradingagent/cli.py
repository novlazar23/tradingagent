"""CLI boundary sharing the same application services as the HTTP API."""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
import yaml

from tradingagent.api.services import ApplicationRegistry
from tradingagent.config import AppConfig, database_url_from_environment
from tradingagent.logging import configure_logging
from tradingagent.market_data.adapter import OctoBotHistoryClient
from tradingagent.persistence.bootstrap import bootstrap_environment
from tradingagent.persistence.handlers import RuntimeHandlerFactory
from tradingagent.persistence.runtime import DurableJobWorker, JobScheduler
from tradingagent.strategy_lab.compiler import compile_freqtrade_strategy
from tradingagent.strategy_lab.extractor import extract_strategy
from tradingagent.strategy_lab.models import StrategySpec, TranscriptSegment
from tradingagent.strategy_lab.whisper import WhisperClient

_DATABASE_URL = database_url_from_environment()
REGISTRY = (
    ApplicationRegistry.from_database_url(_DATABASE_URL) if _DATABASE_URL else ApplicationRegistry()
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradingagent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve-api", help="run the internal FastAPI service")
    commands.add_parser("run-worker", help="run the isolated background worker")
    commands.add_parser("run-scheduler", help="run the isolated scheduler")
    bootstrap = commands.add_parser("bootstrap", help="register dataset and configuration")
    bootstrap.add_argument("--external-dataset-id", required=True)
    bootstrap.add_argument("--start", required=True)
    bootstrap.add_argument("--end", required=True)

    config = commands.add_parser("config", help="configuration operations").add_subparsers(
        dest="action", required=True
    )
    validate = config.add_parser("validate")
    validate.add_argument("path", type=Path)

    data = commands.add_parser("data", help="market-data operations").add_subparsers(
        dest="action", required=True
    )
    data.add_parser("datasets")
    data.add_parser("source-datasets")
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

    strategy = commands.add_parser(
        "strategy", help="video-derived strategy operations"
    ).add_subparsers(dest="action", required=True)
    transcribe = strategy.add_parser("transcribe")
    transcribe.add_argument("--url", required=True)
    transcribe.add_argument("--output", type=Path, required=True)
    transcribe.add_argument("--model", default="small")
    transcribe.add_argument("--language")
    extract = strategy.add_parser("extract")
    extract.add_argument("--transcript", type=Path, required=True)
    extract.add_argument("--name", required=True)
    extract.add_argument("--timeframe", default="1h")
    extract.add_argument("--output", type=Path, required=True)
    compile_command = strategy.add_parser("compile")
    compile_command.add_argument("--spec", type=Path, required=True)
    compile_command.add_argument("--output", type=Path, required=True)

    database = commands.add_parser("db", help="database operations").add_subparsers(
        dest="action", required=True
    )
    database.add_parser("migrate")
    return parser


def _print(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(exclude_none=True)
    print(json.dumps(value, sort_keys=True, default=str))


def _runtime_config() -> AppConfig:
    path = Path(__import__("os").environ.get("TRADINGAGENT_CONFIG", "/app/config/config.yaml"))
    return AppConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _runtime_history(config: AppConfig) -> OctoBotHistoryClient:
    return OctoBotHistoryClient(
        str(config.deployment.history_base_url),
        config.deployment.history_api_key_file,
    )


def _runtime_handlers(config: AppConfig | None = None) -> dict[str, Any]:
    config = config or _runtime_config()
    return RuntimeHandlerFactory(
        REGISTRY,
        config=config,
        history=_runtime_history(config),
        code_version=__import__("os").environ.get("TRADINGAGENT_CODE_VERSION", "development"),
    ).handlers()


def main(argv: list[str] | None = None) -> int:
    """Dispatch a command and return a process exit code."""
    configure_logging()
    args = build_parser().parse_args(argv)
    if args.command == "serve-api":
        uvicorn.run("tradingagent.api.app:app", host="0.0.0.0", port=8000)
        return 0
    if args.command == "run-worker":
        worker = DurableJobWorker(REGISTRY, _runtime_handlers())
        while True:
            if not worker.run_once():
                time.sleep(1)
    if args.command == "run-scheduler":
        config = _runtime_config()
        scheduler = JobScheduler(REGISTRY, interval=timedelta(minutes=15))
        while True:
            scheduler.enqueue_due()
            scheduler.enqueue_data_syncs()
            time.sleep(config.deployment.paper_poll_seconds)
    if args.command == "bootstrap":
        config = _runtime_config()
        result = bootstrap_environment(
            REGISTRY,
            _runtime_history(config),
            config,
            external_dataset_id=args.external_dataset_id,
            start=datetime.fromisoformat(args.start.replace("Z", "+00:00")),
            end=datetime.fromisoformat(args.end.replace("Z", "+00:00")),
            code_version=__import__("os").environ.get("TRADINGAGENT_CODE_VERSION", "development"),
        )
        _print(result)
        return 0
    if args.command == "config":
        payload = yaml.safe_load(args.path.read_text())
        AppConfig.model_validate(payload)
        _print({"valid": True, "path": str(args.path)})
        return 0
    if args.command == "data":
        if args.action == "datasets":
            _print({"datasets": REGISTRY.datasets})
        elif args.action == "source-datasets":
            config = _runtime_config()
            _print({"datasets": _runtime_history(config).list_datasets()})
        elif args.action == "sync":
            _print(REGISTRY.create_job("data_sync", {"dataset_id": args.dataset_id}))
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
    if args.command == "strategy":
        if args.action == "transcribe":
            base_url = __import__("os").environ.get("TRANSCRIPTION_BASE_URL", "http://whisper:8080")
            transcript = WhisperClient(base_url, model=args.model).transcribe(
                args.url, language=args.language
            )
            args.output.write_text(
                json.dumps(
                    {
                        "source_url": args.url,
                        "language": transcript.language,
                        "segments": [segment.model_dump() for segment in transcript.segments],
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _print({"output": str(args.output), "segments": len(transcript.segments)})
        elif args.action == "extract":
            source = json.loads(args.transcript.read_text(encoding="utf-8"))
            segments = tuple(
                TranscriptSegment.model_validate(segment) for segment in source["segments"]
            )
            spec = extract_strategy(
                name=args.name,
                source_url=str(source["source_url"]),
                segments=segments,
                timeframe=args.timeframe,
            )
            args.output.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
            _print({"status": spec.status, "strategy": spec.name, "output": str(args.output)})
        else:
            spec = StrategySpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
            args.output.write_text(compile_freqtrade_strategy(spec), encoding="utf-8")
            _print({"output": str(args.output), "strategy": spec.name})
        return 0
    if args.command == "db":
        return subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            check=False,
        ).returncode
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
