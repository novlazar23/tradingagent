from types import SimpleNamespace

import pytest

import tradingagent.cli as cli
from tradingagent.cli import build_parser, main


def test_cli_exposes_required_commands(capsys: object) -> None:
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    for command in ("bootstrap", "config", "data", "backtest", "paper", "strategy", "db"):
        assert command in output


def test_cli_data_datasets_uses_application_service(capsys: object) -> None:
    assert main(["data", "datasets"]) == 0
    assert '"datasets": []' in capsys.readouterr().out  # type: ignore[attr-defined]


def test_cli_source_datasets_reads_the_octobot_catalog(
    monkeypatch: pytest.MonkeyPatch, capsys: object
) -> None:
    history = SimpleNamespace(list_datasets=lambda: ("one.data", "two.data"))
    monkeypatch.setattr(cli, "_runtime_config", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "_runtime_history", lambda _config: history)

    assert main(["data", "source-datasets"]) == 0
    assert '"one.data"' in capsys.readouterr().out  # type: ignore[attr-defined]


def test_cli_data_sync_passes_required_dataset_to_job(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def create_job(kind: str, payload: dict[str, object]):
        captured.update(kind=kind, payload=payload)
        return {"id": "job"}

    monkeypatch.setattr(cli.REGISTRY, "create_job", create_job)

    assert main(["data", "sync", "--dataset-id", "btc"]) == 0
    assert captured == {"kind": "data_sync", "payload": {"dataset_id": "btc"}}


def test_cli_bootstrap_requires_explicit_dataset_and_utc_range(
    monkeypatch: pytest.MonkeyPatch, capsys: object
) -> None:
    captured: dict[str, object] = {}

    def bootstrap(*_args: object, **kwargs: object) -> dict[str, str]:
        captured.update(kwargs)
        return {"dataset_id": "internal", "configuration_version": "version"}

    monkeypatch.setattr(cli, "bootstrap_environment", bootstrap)
    monkeypatch.setattr(cli, "_runtime_config", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "_runtime_history", lambda _config: object())

    assert (
        main(
            [
                "bootstrap",
                "--external-dataset-id",
                "btc.data",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-07-01T00:00:00Z",
            ]
        )
        == 0
    )
    assert captured["external_dataset_id"] == "btc.data"
    assert captured["start"].isoformat() == "2026-01-01T00:00:00+00:00"  # type: ignore[union-attr]
    assert '"configuration_version": "version"' in capsys.readouterr().out  # type: ignore[attr-defined]


def test_scheduler_uses_typed_paper_poll_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []

    class Scheduler:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def enqueue_due(self) -> None:
            pass

        def enqueue_data_syncs(self) -> None:
            pass

    monkeypatch.setattr(
        cli,
        "_runtime_config",
        lambda: SimpleNamespace(deployment=SimpleNamespace(paper_poll_seconds=7)),
    )
    monkeypatch.setattr(cli, "JobScheduler", Scheduler)

    def sleep(seconds: int) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        main(["run-scheduler"])
    assert sleeps == [7]


def test_cli_paper_requires_explicit_subcommand() -> None:
    try:
        main(["paper"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("paper without a subcommand must fail")


def test_strategy_commands_have_stable_parse_contract() -> None:
    transcribe = build_parser().parse_args(
        ["strategy", "transcribe", "--url", "https://youtu.be/example", "--output", "t.json"]
    )
    compile_ = build_parser().parse_args(
        ["strategy", "compile", "--spec", "spec.json", "--output", "Strategy.py"]
    )
    extract = build_parser().parse_args(
        [
            "strategy",
            "extract",
            "--transcript",
            "transcript.json",
            "--name",
            "rsi_video",
            "--output",
            "spec.json",
        ]
    )

    assert (transcribe.command, transcribe.action) == ("strategy", "transcribe")
    assert (compile_.command, compile_.action) == ("strategy", "compile")
    assert (extract.command, extract.action) == ("strategy", "extract")


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "validate", "strategy.yaml"],
        [
            "bootstrap",
            "--external-dataset-id",
            "btc.data",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-07-01T00:00:00Z",
        ],
        ["data", "datasets"],
        ["data", "source-datasets"],
        ["data", "sync", "--dataset-id", "btc"],
        ["data", "gaps", "--dataset-id", "btc"],
        ["backtest", "run", "--dataset-id", "btc", "--configuration-version", "v1"],
        ["backtest", "report", "run-id"],
        ["paper", "create", "--dataset-id", "btc", "--configuration-version", "v1"],
        ["paper", "start", "session-id"],
        ["paper", "pause", "session-id"],
        ["paper", "resume", "session-id"],
        ["paper", "stop", "session-id"],
        ["paper", "status", "session-id"],
        ["db", "migrate"],
    ],
)
def test_every_documented_cli_command_has_a_stable_parse_contract(argv: list[str]) -> None:
    parsed = build_parser().parse_args(argv)
    assert parsed.command == argv[0]


@pytest.mark.parametrize("argv", [["data", "sync"], ["backtest", "report"], ["paper", "start"]])
def test_cli_missing_required_arguments_use_stable_nonzero_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as captured:
        build_parser().parse_args(argv)
    assert captured.value.code == 2
    assert "required" in capsys.readouterr().err
