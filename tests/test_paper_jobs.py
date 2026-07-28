from datetime import UTC, datetime

from tradingagent.paper import InMemoryJobRepository, JobRunner, JobStatus

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def test_job_idempotency_returns_same_observable_result() -> None:
    repository = InMemoryJobRepository()
    runner = JobRunner(repository)
    calls = 0

    def work(progress):
        nonlocal calls
        calls += 1
        progress(2, 3)
        return {"processed": 2}

    first = runner.run("paper-cycle", "same-key", work, now=NOW)
    repeated = runner.run("paper-cycle", "same-key", work, now=NOW)

    assert repeated == first
    assert calls == 1
    assert first.status is JobStatus.SUCCEEDED
    assert first.completed_units == 2
    assert first.total_units == 3


def test_retrying_job_records_retry_count_and_terminal_error() -> None:
    repository = InMemoryJobRepository()
    runner = JobRunner(repository, maximum_retries=2)

    def fails(_progress):
        raise ConnectionError("history unavailable")

    result = runner.run("paper-cycle", "failure-key", fails, now=NOW)

    assert result.status is JobStatus.FAILED
    assert result.retry_count == 2
    assert result.terminal_error == "ConnectionError: history unavailable"
