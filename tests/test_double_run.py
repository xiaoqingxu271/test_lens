from __future__ import annotations

from helpers import make_profile, sandbox_config
from sandbox import double_run
from sandbox.runner import (
    FailureKind,
    FinalStatus,
    ResourceUsage,
    TestOutcome as Outcome,
    TestRunResult as RunResult,
    TestStatus as Status,
)


def _synthetic_run(
    index: int,
    status: Status,
    failure_kind: FailureKind,
) -> RunResult:
    return RunResult(
        run_index=index,
        started_at="2026-01-01T00:00:00+00:00",
        elapsed_ms=10,
        exit_code=0 if status == Status.PASSED else 1,
        timed_out=False,
        tests=[
            Outcome(
                test_id="test_x.py::test_x",
                test_file="test_x.py",
                test_name="test_x",
                status=status,
                failure_kind=failure_kind,
                exception_type=(
                    "AssertionError"
                    if failure_kind == FailureKind.ASSERTION
                    else None
                ),
                message_digest=(
                    "deadbeef" if failure_kind == FailureKind.ASSERTION else None
                ),
            )
        ],
        resource_usage=ResourceUsage(wall_time_ms=10),
    )


def test_double_run_stable_is_not_flaky():
    report = double_run.execute(
        make_profile("test_strong.py"),
        sandbox_config(double_run=True),
    )

    assert len(report.runs) == 2
    assert len(report.tests) == 1
    assert report.flaky == []
    assert report.tests[0].final_status == FinalStatus.PASSED
    assert report.tests[0].flaky is False
    assert len(report.tests[0].runs) == 2
    assert report.trace.double_run is True


def test_double_run_detects_flaky_from_disagreeing_runs():
    report = double_run.merge_runs(
        [
            _synthetic_run(0, Status.PASSED, FailureKind.NONE),
            _synthetic_run(1, Status.FAILED, FailureKind.ASSERTION),
        ],
        sandbox_config(double_run=True),
        "synthetic",
    )

    assert len(report.flaky) == 1
    record = report.flaky[0]
    assert record.test_id == "test_x.py::test_x"
    assert record.run_statuses == [Status.PASSED, Status.FAILED]
    assert record.consistent is False
    assert record.failure_details_by_run[1].message_digest == "deadbeef"

    assert len(report.tests) == 1
    assert report.tests[0].final_status == FinalStatus.FLAKY
    assert report.tests[0].flaky is True
    assert report.tests[0].failure_kinds == [
        FailureKind.NONE,
        FailureKind.ASSERTION,
    ]


def test_double_run_consistent_failure_is_not_flaky():
    report = double_run.merge_runs(
        [
            _synthetic_run(0, Status.FAILED, FailureKind.ASSERTION),
            _synthetic_run(1, Status.FAILED, FailureKind.ASSERTION),
        ],
        sandbox_config(double_run=True),
        "synthetic",
    )

    assert report.flaky == []
    assert report.tests[0].final_status == FinalStatus.FAILED
    assert report.tests[0].flaky is False


def test_double_run_off_skips_second_run():
    report = double_run.execute(
        make_profile("test_strong.py"),
        sandbox_config(double_run=False),
    )

    assert len(report.runs) == 1
    assert report.trace.double_run is False
    assert report.flaky == []
