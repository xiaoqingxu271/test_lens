"""Target-local mutation scoring for candidates that passed the sandbox."""

from __future__ import annotations

from pathlib import Path

from mutation.operator import DEFAULT_OPERATORS, OperatorId
from mutation.score import MutationScoreReport, score_test
from sandbox.runner import (
    ExecutionReport,
    FinalStatus,
    ProjectProfile,
    SandboxConfig,
)
from targets.models import TargetSelectionReport

from .attribute import covers_target
from .execute import target_symbols_by_id
from .materialize import PipelineError
from .models import MaterializedProject


def score_candidates(
    profile: ProjectProfile,
    materialized: MaterializedProject,
    selection: TargetSelectionReport,
    reports: dict[str, ExecutionReport],
    *,
    operators: tuple[OperatorId, ...] = DEFAULT_OPERATORS,
    seed: int = 0,
    sandbox_config: SandboxConfig | None = None,
) -> dict[str, MutationScoreReport]:
    targets = target_symbols_by_id(selection)
    project_root = Path(materialized.project_root)
    scoring_profile = ProjectProfile(
        profile_id=f"{profile.profile_id}::mutation",
        root=project_root,
        test_paths=[],
        target_symbols=[],
        python_executable=profile.python_executable,
        env=dict(profile.env),
    )

    scored: dict[str, MutationScoreReport] = {}
    for test in materialized.tests:
        report = reports.get(test.test_id)
        if report is None:
            raise PipelineError(f"candidate {test.test_id} was not executed")
        if len(report.tests) != 1:
            continue
        test_result = report.tests[0]
        if test_result.final_status != FinalStatus.PASSED:
            continue
        if not covers_target(test_result, test.target_symbol):
            continue

        target = targets.get(test.target_symbol)
        if target is None:
            raise PipelineError(
                f"candidate {test.test_id} references unselected target "
                f"{test.target_symbol}"
            )

        mutation = score_test(
            scoring_profile,
            target,
            Path(test.path),
            operators=operators,
            sandbox_config=sandbox_config,
            seed=seed,
        )
        if mutation.test_id != test.test_id:
            raise PipelineError(
                f"mutation report test id {mutation.test_id!r} did not match "
                f"candidate {test.test_id!r}"
            )
        scored[test.test_id] = mutation
    return scored
