"""Double-run execution and flaky detection.

A test is flaky only when its pass/fail status differs between two independent
runs. Failure categories are retained for diagnosis but never decide flakiness.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import runner
from .runner import (
    ExecutionReport,
    ExecutionTrace,
    FailureDetail,
    FinalStatus,
    FlakyRecord,
    ProjectProfile,
    ResourceSummary,
    ResourceUsage,
    RunnerError,
    RunnerStage,
    SandboxConfig,
    TestOutcome,
    TestResult,
    TestRunResult,
    TestStatus,
    TimeoutInfo,
    TimeoutSummary,
)


def _aggregate_resource_usage(
    usages: list[ResourceUsage], config: SandboxConfig
) -> ResourceUsage:
    cpu_values = [u.cpu_time_seconds for u in usages if u.cpu_time_seconds is not None]
    rss_values = [u.peak_rss_mb for u in usages if u.peak_rss_mb is not None]
    limits = sorted({item for usage in usages for item in usage.limits_hit})
    return ResourceUsage(
        wall_time_ms=max((u.wall_time_ms for u in usages), default=0),
        cpu_time_seconds=max(cpu_values) if cpu_values else None,
        peak_rss_mb=max(rss_values) if rss_values else None,
        limits_hit=limits,
        enforcement_mode=config.enforcement_mode,
    )


def _resource_summary(
    runs: list[TestRunResult], config: SandboxConfig
) -> ResourceSummary:
    cpu_values = [
        run.resource_usage.cpu_time_seconds
        for run in runs
        if run.resource_usage.cpu_time_seconds is not None
    ]
    rss_values = [
        run.resource_usage.peak_rss_mb
        for run in runs
        if run.resource_usage.peak_rss_mb is not None
    ]
    limits = sorted(
        {item for run in runs for item in run.resource_usage.limits_hit}
    )
    return ResourceSummary(
        peak_rss_mb=max(rss_values) if rss_values else None,
        cpu_time_seconds=sum(cpu_values) if cpu_values else None,
        wall_time_ms=sum(run.resource_usage.wall_time_ms for run in runs),
        limits_hit=limits,
        enforcement_mode=config.enforcement_mode,
        platform=config.platform,
    )


def _timeout_summary(
    tests: list[TestResult], runs: list[TestRunResult], config: SandboxConfig
) -> TimeoutSummary:
    timed_out_ids = sorted(
        {
            test.test_id
            for test in tests
            if test.final_status == FinalStatus.TIMEOUT
            or any(outcome.status == TestStatus.TIMEOUT for outcome in test.runs)
        }
    )
    any_timeout = bool(timed_out_ids) or any(run.timed_out for run in runs)
    return TimeoutSummary(
        any_timeout=any_timeout,
        timed_out_test_ids=timed_out_ids,
        timeout_seconds=config.timeout_seconds,
    )


def merge_runs(
    runs: list[TestRunResult],
    config: SandboxConfig,
    project_profile_id: str,
    *,
    runner_errors: list[RunnerError] | None = None,
    warnings: list[str] | None = None,
    run_id: str | None = None,
    started_at: str | None = None,
    elapsed_ms: int | None = None,
    cwd: str = "",
    env_digest: str = "",
) -> ExecutionReport:
    if not runs:
        raise ValueError("at least one run is required")

    runner_errors = list(runner_errors or [])
    warnings = list(warnings or [])
    config = runner.resolve_config(config)
    run_id = run_id or uuid.uuid4().hex
    started_at = started_at or datetime.now(timezone.utc).isoformat()
    if elapsed_ms is None:
        elapsed_ms = sum(run.elapsed_ms for run in runs)

    by_test: dict[str, list[tuple[int, TestOutcome]]] = {}
    for run in runs:
        for outcome in run.tests:
            by_test.setdefault(outcome.test_id, []).append((run.run_index, outcome))

    tests: list[TestResult] = []
    flaky_records: list[FlakyRecord] = []

    for test_id in sorted(by_test):
        indexed = sorted(by_test[test_id], key=lambda pair: pair[0])
        outcomes = [outcome for _, outcome in indexed]
        missing = len(outcomes) != len(runs)
        if missing:
            runner_errors.append(
                RunnerError(
                    stage=RunnerStage.EXECUTION,
                    message=f"missing result for {test_id} in one of the runs",
                    fatal=False,
                )
            )

        statuses = [outcome.status for outcome in outcomes]
        flaky = False
        if not missing and len(statuses) > 1:
            flaky = statuses[0] != statuses[1]

        if missing:
            final_status = FinalStatus.ERROR
        elif flaky:
            final_status = FinalStatus.FLAKY
        else:
            final_status = FinalStatus(statuses[0].value)

        covered = sorted(
            {
                symbol.symbol_id
                for outcome in outcomes
                for symbol in outcome.covered_target_symbols
            }
        )
        target_symbol = covered[0] if len(covered) == 1 else None

        timeout = next(
            (outcome.timeout for outcome in outcomes if outcome.timeout.timed_out),
            outcomes[0].timeout if outcomes else TimeoutInfo(timeout_seconds=config.timeout_seconds),
        )
        resource_usage = _aggregate_resource_usage(
            [outcome.resource_usage for outcome in outcomes], config
        )

        test_result = TestResult(
            test_id=test_id,
            test_file=outcomes[0].test_file if outcomes else "",
            test_name=outcomes[0].test_name if outcomes else test_id,
            target_symbol=target_symbol,
            final_status=final_status,
            runs=outcomes,
            flaky=flaky,
            failure_kinds=[outcome.failure_kind for outcome in outcomes],
            covered_target_symbols_union=covered,
            timeout=timeout,
            resource_usage=resource_usage,
        )
        tests.append(test_result)

        if flaky:
            flaky_records.append(
                FlakyRecord(
                    test_id=test_id,
                    test_file=test_result.test_file,
                    test_name=test_result.test_name,
                    run_statuses=statuses,
                    consistent=False,
                    failure_details_by_run=[
                        FailureDetail(
                            run_index=indexed[index][0],
                            failure_kind=outcomes[index].failure_kind,
                            exception_type=outcomes[index].exception_type,
                            message_digest=outcomes[index].message_digest,
                        )
                        for index in range(len(outcomes))
                    ],
                )
            )

    trace = ExecutionTrace(
        double_run=config.double_run,
        cwd=cwd,
        env_digest=env_digest,
        warnings=sorted(set(warnings)),
    )
    return ExecutionReport(
        run_id=run_id,
        started_at=started_at,
        elapsed_ms=elapsed_ms,
        project_profile_id=project_profile_id,
        sandbox_config=config,
        runs=runs,
        tests=tests,
        flaky=flaky_records,
        resource_summary=_resource_summary(runs, config),
        timeout_summary=_timeout_summary(tests, runs, config),
        trace=trace,
        runner_errors=runner_errors,
    )


def execute(
    profile: ProjectProfile,
    config: SandboxConfig,
    *,
    artifact_root: Path | None = None,
) -> ExecutionReport:
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    resolved = runner.resolve_config(config)
    attempts = 2 if resolved.double_run else 1

    runs: list[TestRunResult] = []
    errors: list[RunnerError] = []
    warnings: list[str] = []

    for index in range(attempts):
        artifact_dir = None
        if artifact_root is not None:
            artifact_dir = Path(artifact_root) / f"run_{index}"
        attempt = runner.run_once(
            profile,
            resolved,
            run_index=index,
            artifact_dir=artifact_dir,
        )
        runs.append(attempt.result)
        errors.extend(attempt.errors)
        warnings.extend(attempt.warnings)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return merge_runs(
        runs,
        resolved,
        profile.profile_id,
        runner_errors=errors,
        warnings=warnings,
        started_at=started_at,
        elapsed_ms=elapsed_ms,
        cwd=str(Path(profile.root).expanduser().resolve()),
        env_digest=runner.env_digest(profile),
    )
