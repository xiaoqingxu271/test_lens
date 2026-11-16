"""Test pipeline orchestration.

Stage order is load-bearing:

``generate_tests`` -> materialize -> execute (double-run) -> mutation score
-> attribute -> ``TestReport``.

Nothing here calls a model or writes to the target project. Line coverage is
never an acceptance criterion; the only acceptance gates are sandbox success
and the mutation-kill threshold.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from generation.models import GenerationReport
from mutation.operator import DEFAULT_OPERATORS, OperatorId
from sandbox import runner
from sandbox.runner import FinalStatus, ProjectProfile, SandboxConfig
from targets.models import TargetSelectionReport

from .attribute import attribute_discard
from .execute import execute_candidates
from .materialize import PipelineError, materialize_tests
from .models import (
    CandidateEvidence,
    TestPipelineTrace,
    TestReport,
)
from .scoring import score_candidates


def run_test_pipeline(
    profile: ProjectProfile,
    selection: TargetSelectionReport,
    generation: GenerationReport,
    *,
    kill_threshold: float = 0.5,
    seed: int = 0,
    operators: tuple[OperatorId, ...] = DEFAULT_OPERATORS,
    sandbox_config: SandboxConfig | None = None,
    workdir_root: Path | None = None,
) -> TestReport:
    if not 0.0 <= kill_threshold <= 1.0:
        raise ValueError("kill_threshold must be between 0 and 1")

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    config = runner.resolve_config(
        sandbox_config or SandboxConfig(double_run=True)
    )
    warnings: set[str] = set()
    if not config.double_run:
        warnings.add(
            "double_run is disabled; this report is excluded from evaluation"
        )

    if not generation.candidates:
        warnings.add("no candidate tests to execute")
        return TestReport(
            trace=TestPipelineTrace(
                started_at=started_at,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                candidates_in=0,
                double_run=config.double_run,
                kill_threshold=kill_threshold,
                operator_ids=[operator.value for operator in operators],
                seed=seed,
                platform=config.platform,
                enforcement_mode=config.enforcement_mode,
                warnings=sorted(warnings),
            )
        )

    owned = workdir_root is None
    base = (
        Path(tempfile.mkdtemp(prefix="testlens_pipeline_"))
        if owned
        else Path(workdir_root).expanduser().resolve()
    )
    if not owned and base.exists() and any(base.iterdir()):
        raise PipelineError(f"workdir_root is not empty: {base}")
    base.mkdir(parents=True, exist_ok=True)

    try:
        materialized = materialize_tests(
            profile,
            generation.candidates,
            base / "project",
            artifacts_root=base / "artifacts",
        )
        reports = execute_candidates(
            profile,
            materialized,
            selection,
            config,
            artifact_root=Path(materialized.artifacts_root),
        )
        mutations = score_candidates(
            profile,
            materialized,
            selection,
            reports,
            operators=operators,
            seed=seed,
            sandbox_config=config,
        )

        candidates_by_id = {
            candidate.test_id: candidate
            for candidate in generation.candidates
        }
        evidence: list[CandidateEvidence] = []
        for test in materialized.tests:
            report = reports[test.test_id]
            test_result = report.tests[0] if len(report.tests) == 1 else None
            evidence.append(
                CandidateEvidence(
                    candidate=candidates_by_id[test.test_id],
                    test_result=test_result,
                    flaky_record=report.flaky[0] if report.flaky else None,
                    mutation=mutations.get(test.test_id),
                    runner_errors=report.runner_errors,
                )
            )

        attribution = attribute_discard(evidence, kill_threshold=kill_threshold)
        warnings.update(attribution.warnings)

        sandbox_passed = sum(
            1
            for item in evidence
            if item.test_result is not None
            and item.test_result.final_status == FinalStatus.PASSED
        )
        scored = list(mutations.values())
        trace = TestPipelineTrace(
            started_at=started_at,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            candidates_in=len(generation.candidates),
            tests_materialized=len(materialized.tests),
            tests_executed=len(reports),
            sandbox_passed=sandbox_passed,
            flaky=len(attribution.flaky),
            mutation_scored=len(scored),
            mutations_run=sum(item.mutations_run for item in scored),
            mutations_killed=sum(item.mutations_killed for item in scored),
            killed_by_assertion=sum(item.killed_by_assertion for item in scored),
            killed_by_exception=sum(item.killed_by_exception for item in scored),
            killed_by_timeout=sum(item.killed_by_timeout for item in scored),
            kept=len(attribution.kept),
            discarded_by_reason=attribution.discarded_by_reason,
            double_run=config.double_run,
            kill_threshold=kill_threshold,
            operator_ids=[operator.value for operator in operators],
            seed=seed,
            platform=config.platform,
            enforcement_mode=config.enforcement_mode,
            warnings=sorted(warnings),
        )
        return TestReport(
            kept=attribution.kept,
            discarded=attribution.discarded,
            flaky=attribution.flaky,
            trace=trace,
        )
    finally:
        if owned:
            shutil.rmtree(base, ignore_errors=True)
