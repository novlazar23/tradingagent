from tradingagent.cli import main


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
