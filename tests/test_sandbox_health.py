from __future__ import annotations

from sandbox import double_run
from sandbox.health import (
    PROBE_SYMBOL_ID,
    STRONG_TEST_NAME,
    WEAK_TEST_NAME,
    SandboxHealthFailure,
    check_sandbox_ready,
    evaluate_probe,
)
from sandbox.runner import (
    CoverageSource,
    FailureKind,
    SandboxConfig,
    SymbolCoverage,
    TestOutcome as Outcome,
    TestRunResult as RunResult,
    TestStatus as Status,
)


def _outcome(
    test_name: str,
    status: Status,
    *,
    covers: bool,
) -> Outcome:
    covered = []
    if covers:
        covered.append(
            SymbolCoverage(
                symbol_id=PROBE_SYMBOL_ID,
                file="app.py",
                line_start=1,
                line_end=6,
                entry_hit=True,
                executed_lines=[2, 3, 4, 5, 6],
                coverage_source=CoverageSource.SYS_SETTRACE,
            )
        )
    return Outcome(
        test_id=f"test_probe.py::{test_name}",
        test_file="test_probe.py",
        test_name=test_name,
        status=status,
        failure_kind=(
            FailureKind.NONE if status == Status.PASSED else FailureKind.ASSERTION
        ),
        covered_target_symbols=covered,
    )


def _run(
    index: int,
    strong_status: Status = Status.PASSED,
    *,
    covers: bool = True,
) -> RunResult:
    return RunResult(
        run_index=index,
        started_at="2026-01-01T00:00:00+00:00",
        elapsed_ms=1,
        exit_code=0 if strong_status == Status.PASSED else 1,
        timed_out=False,
        tests=[
            _outcome(STRONG_TEST_NAME, strong_status, covers=covers),
            _outcome(WEAK_TEST_NAME, Status.PASSED, covers=False),
        ],
    )


def _report(*runs: RunResult):
    return double_run.merge_runs(
        list(runs),
        SandboxConfig(double_run=True),
        "probe",
    )


def test_health_check_accepts_a_working_sandbox():
    health = check_sandbox_ready(timeout_seconds=30.0)

    assert health.ready is True, health.details
    assert health.strong_test_passed is True
    assert health.strong_covers_target is True
    assert health.weak_test_passed is True
    assert health.double_run is True
    assert health.tests_reported == 2
    assert health.failures == []


def test_evaluate_probe_accepts_a_healthy_report():
    health = evaluate_probe(_report(_run(0), _run(1)))

    assert health.ready is True
    assert health.failures == []
    assert health.strong_covers_target is True


def test_evaluate_probe_reports_failed_strong_test_and_missing_coverage():
    health = evaluate_probe(
        _report(
            _run(0, Status.FAILED, covers=False),
            _run(1, Status.FAILED, covers=False),
        )
    )

    assert health.ready is False
    assert SandboxHealthFailure.STRONG_TEST_FAILED in health.failures
    assert SandboxHealthFailure.STRONG_TEST_NO_COVERAGE in health.failures
    assert any(STRONG_TEST_NAME in detail for detail in health.details)


def test_evaluate_probe_requires_two_runs():
    health = evaluate_probe(_report(_run(0)))

    assert health.ready is False
    assert SandboxHealthFailure.DOUBLE_RUN_NOT_APPLIED in health.failures
    assert health.double_run is True
