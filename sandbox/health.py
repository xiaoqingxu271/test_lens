"""Sandbox acceptance probe.

Test generation may not start unless the executor is available and passes its
own acceptance checks. The probe runs a strong and a weak test through the
same double-run path used for candidates, then evaluates the report:

- the strong test must pass and cover the probe symbol;
- the weak test must pass (weakness is judged later by mutation scoring);
- both runs must be present and no runner errors or flaky disagreement may
  occur.
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from . import double_run
from .container import (
    ContainerFailure,
    SandboxBackend,
    check_available,
)
from .runner import (
    EnforcementMode,
    ExecutionReport,
    FinalStatus,
    ProjectProfile,
    RUNNER_VERSION,
    SandboxConfig,
    TargetSymbol,
    resolve_config,
)

PROBE_SYMBOL_ID = "classify"
STRONG_TEST_NAME = "test_classify_strong"
WEAK_TEST_NAME = "test_classify_weak"
PROBE_DOUBLE_RUN = True

PROBE_APP_SOURCE = """\
def classify(x):
    if x > 0:
        return 1
    if x == 0:
        return 0
    return -1
"""

PROBE_STRONG_SOURCE = """\
from app import classify


def test_classify_strong():
    assert classify(1) == 1
    assert classify(0) == 0
    assert classify(-1) == -1
"""

PROBE_WEAK_SOURCE = """\
from app import classify


def test_classify_weak():
    classify(1)
    classify(0)
    classify(-1)
"""


class SandboxHealthFailure(str, Enum):
    RUNNER_ERROR = "runner_error"
    NO_TESTS = "no_tests"
    STRONG_TEST_FAILED = "strong_test_failed"
    STRONG_TEST_NO_COVERAGE = "strong_test_no_coverage"
    WEAK_TEST_FAILED = "weak_test_failed"
    DOUBLE_RUN_NOT_APPLIED = "double_run_not_applied"
    FLAKY_PROBE = "flaky_probe"
    CONTAINER_RUNTIME_UNAVAILABLE = "container_runtime_unavailable"
    CONTAINER_IMAGE_UNAVAILABLE = "container_image_unavailable"
    CONTAINER_PROBE_FAILED = "container_probe_failed"
    CONTAINER_UNSUPPORTED_PLATFORM = "container_unsupported_platform"


class SandboxHealth(BaseModel):
    ready: bool
    checked_at: str
    elapsed_ms: int
    platform: str
    enforcement_mode: EnforcementMode
    runner_version: str = RUNNER_VERSION
    double_run: bool = PROBE_DOUBLE_RUN
    backend: SandboxBackend = SandboxBackend.SUBPROCESS
    tests_reported: int = 0
    strong_test_passed: bool = False
    strong_covers_target: bool = False
    weak_test_passed: bool = False
    failures: list[SandboxHealthFailure] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)


def _strong_coverage(report: ExecutionReport) -> bool:
    for test in report.tests:
        if test.test_name != STRONG_TEST_NAME:
            continue
        for outcome in test.runs:
            for symbol in outcome.covered_target_symbols:
                if symbol.symbol_id == PROBE_SYMBOL_ID and symbol.entry_hit:
                    if symbol.executed_lines:
                        return True
    return False


def evaluate_probe(report: ExecutionReport) -> SandboxHealth:
    """Evaluate a probe execution report without touching the filesystem."""
    failures: list[SandboxHealthFailure] = []
    details: list[str] = []

    if report.runner_errors:
        failures.append(SandboxHealthFailure.RUNNER_ERROR)
        details.extend(
            f"{error.stage.value}: {error.message}" for error in report.runner_errors
        )
    if len(report.runs) != 2:
        failures.append(SandboxHealthFailure.DOUBLE_RUN_NOT_APPLIED)
        details.append(f"expected 2 runs, got {len(report.runs)}")

    by_name = {test.test_name: test for test in report.tests}
    strong = by_name.get(STRONG_TEST_NAME)
    weak = by_name.get(WEAK_TEST_NAME)
    if strong is None or weak is None:
        failures.append(SandboxHealthFailure.NO_TESTS)
        missing = [
            name
            for name, test in (
                (STRONG_TEST_NAME, strong),
                (WEAK_TEST_NAME, weak),
            )
            if test is None
        ]
        details.append(f"missing probe tests: {', '.join(missing)}")

    strong_passed = strong is not None and strong.final_status == FinalStatus.PASSED
    if not strong_passed:
        failures.append(SandboxHealthFailure.STRONG_TEST_FAILED)
        if strong is not None:
            details.append(
                f"{STRONG_TEST_NAME} final status: {strong.final_status.value}"
            )

    covers = _strong_coverage(report)
    if not covers:
        failures.append(SandboxHealthFailure.STRONG_TEST_NO_COVERAGE)
        details.append(f"{STRONG_TEST_NAME} did not enter {PROBE_SYMBOL_ID}")

    weak_passed = weak is not None and weak.final_status == FinalStatus.PASSED
    if not weak_passed:
        failures.append(SandboxHealthFailure.WEAK_TEST_FAILED)
        if weak is not None:
            details.append(f"{WEAK_TEST_NAME} final status: {weak.final_status.value}")

    if report.flaky:
        failures.append(SandboxHealthFailure.FLAKY_PROBE)
        details.extend(f"flaky probe test: {record.test_id}" for record in report.flaky)

    return SandboxHealth(
        ready=not failures,
        checked_at=report.started_at,
        elapsed_ms=report.elapsed_ms,
        platform=report.sandbox_config.platform,
        enforcement_mode=report.sandbox_config.enforcement_mode,
        runner_version=report.trace.runner_version,
        double_run=report.sandbox_config.double_run,
        backend=report.sandbox_config.backend,
        tests_reported=len(report.tests),
        strong_test_passed=strong_passed,
        strong_covers_target=covers,
        weak_test_passed=weak_passed,
        failures=failures,
        details=details,
    )


def _container_health_failure(
    failure: ContainerFailure | None,
) -> SandboxHealthFailure:
    return {
        ContainerFailure.RUNTIME_UNAVAILABLE: (
            SandboxHealthFailure.CONTAINER_RUNTIME_UNAVAILABLE
        ),
        ContainerFailure.IMAGE_UNAVAILABLE: (
            SandboxHealthFailure.CONTAINER_IMAGE_UNAVAILABLE
        ),
        ContainerFailure.UNSUPPORTED_PLATFORM: (
            SandboxHealthFailure.CONTAINER_UNSUPPORTED_PLATFORM
        ),
        ContainerFailure.PROBE_FAILED: SandboxHealthFailure.CONTAINER_PROBE_FAILED,
    }.get(failure, SandboxHealthFailure.CONTAINER_PROBE_FAILED)


def check_sandbox_ready(
    *,
    timeout_seconds: float = 30.0,
    config: SandboxConfig | None = None,
) -> SandboxHealth:
    """Run the acceptance probe in an isolated temporary project."""
    resolved = resolve_config(
        (config or SandboxConfig()).model_copy(
            update={"timeout_seconds": timeout_seconds, "double_run": True}
        )
    )
    if resolved.backend == SandboxBackend.CONTAINER:
        if resolved.container is None:
            return SandboxHealth(
                ready=False,
                checked_at=datetime.now(timezone.utc).isoformat(),
                elapsed_ms=0,
                platform=resolved.platform,
                enforcement_mode=resolved.enforcement_mode,
                backend=resolved.backend,
                failures=[SandboxHealthFailure.CONTAINER_PROBE_FAILED],
                details=["container backend requires container configuration"],
            )
        availability = check_available(resolved.container)
        if not availability.available:
            return SandboxHealth(
                ready=False,
                checked_at=datetime.now(timezone.utc).isoformat(),
                elapsed_ms=0,
                platform=resolved.platform,
                enforcement_mode=resolved.enforcement_mode,
                backend=resolved.backend,
                failures=[_container_health_failure(availability.failure)],
                details=[availability.detail],
            )

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="testlens_health_") as tmp:
        root = Path(tmp)
        (root / "app.py").write_text(PROBE_APP_SOURCE, encoding="utf-8")
        strong_path = root / "test_strong.py"
        strong_path.write_text(PROBE_STRONG_SOURCE, encoding="utf-8")
        weak_path = root / "test_weak.py"
        weak_path.write_text(PROBE_WEAK_SOURCE, encoding="utf-8")

        profile = ProjectProfile(
            profile_id="sandbox-health",
            root=root,
            test_paths=[strong_path, weak_path],
            target_symbols=[
                TargetSymbol(
                    symbol_id=PROBE_SYMBOL_ID,
                    name=PROBE_SYMBOL_ID,
                    file=root / "app.py",
                    line_start=1,
                    line_end=PROBE_APP_SOURCE.count("\n"),
                )
            ],
        )
        report = double_run.execute(
            profile,
            resolved.model_copy(update={"double_run": PROBE_DOUBLE_RUN}),
            artifact_root=root / "artifacts",
        )
    health = evaluate_probe(report)
    return health.model_copy(
        update={
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    )
