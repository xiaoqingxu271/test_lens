"""Per-candidate sandbox execution.

One subprocess per candidate test; double-run uses two subprocesses. Running
candidates separately keeps a crash, collection error, or timeout in one test
from affecting the verdict of another.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from sandbox import double_run
from sandbox.runner import (
    ExecutionReport,
    ProjectProfile,
    SandboxConfig,
    TargetSymbol,
)
from targets.models import TargetSelectionReport

from .materialize import PipelineError
from .models import MaterializedProject


def target_symbols_by_id(
    selection: TargetSelectionReport,
) -> dict[str, TargetSymbol]:
    return {
        target.symbol_id: TargetSymbol(
            symbol_id=target.symbol_id,
            name=target.name,
            file=Path(target.relative_file),
            line_start=target.line_start,
            line_end=target.line_end,
        )
        for target in selection.targets
    }


def _target_for(
    test, targets: dict[str, TargetSymbol]
) -> TargetSymbol:
    target = targets.get(test.target_symbol)
    if target is None:
        raise PipelineError(
            f"candidate {test.test_id} references unselected target "
            f"{test.target_symbol}"
        )
    return target


def _artifact_slug(test_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", test_id).strip("_")[:60]
    digest = hashlib.sha256(test_id.encode("utf-8")).hexdigest()[:8]
    return f"{safe}_{digest}" if safe else digest


def _validate_report(test_id: str, report: ExecutionReport) -> None:
    reported = {test.test_id for test in report.tests}
    if not reported:
        return  # collection/import failure: attribution maps this to a discard
    if reported != {test_id}:
        raise PipelineError(
            f"execution report for {test_id} reported unexpected tests: "
            + ", ".join(sorted(reported))
        )


def execute_candidates(
    profile: ProjectProfile,
    materialized: MaterializedProject,
    selection: TargetSelectionReport,
    config: SandboxConfig,
    *,
    artifact_root: Path | None = None,
) -> dict[str, ExecutionReport]:
    targets = target_symbols_by_id(selection)
    artifacts = (
        Path(artifact_root).expanduser().resolve()
        if artifact_root is not None
        else Path(materialized.artifacts_root)
    )
    project_root = Path(materialized.project_root)

    reports: dict[str, ExecutionReport] = {}
    for test in materialized.tests:
        target = _target_for(test, targets)
        candidate_profile = ProjectProfile(
            profile_id=f"pipeline::{test.test_id}",
            root=project_root,
            test_paths=[Path(test.path)],
            target_symbols=[target],
            python_executable=profile.python_executable,
            env=dict(profile.env),
        )
        report = double_run.execute(
            candidate_profile,
            config,
            artifact_root=artifacts / _artifact_slug(test.test_id),
        )
        _validate_report(test.test_id, report)
        reports[test.test_id] = report
    return reports
