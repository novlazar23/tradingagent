import pytest

from tradingagent.cli import build_parser, main


def test_cli_exposes_required_commands(capsys: object) -> None:
    try:
        main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    for command in ("config", "data", "backtest", "paper", "db"):
        assert command in output


def test_cli_data_datasets_uses_application_service(capsys: object) -> None:
    assert main(["data", "datasets"]) == 0
    assert '"datasets": []' in capsys.readouterr().out  # type: ignore[attr-defined]


def test_cli_paper_requires_explicit_subcommand() -> None:
    try:
        main(["paper"])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("paper without a subcommand must fail")


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "validate", "strategy.yaml"],
        ["data", "datasets"],
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
