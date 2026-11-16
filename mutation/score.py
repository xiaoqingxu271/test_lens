"""Mutation scoring with an explicit kill-rate breakdown."""

from __future__ import annotations

import ast
import hashlib
import shutil
import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from sandbox import runner
from sandbox.runner import (
    FailureKind,
    ProjectProfile,
    SandboxConfig,
    TargetSymbol,
    TestOutcome,
    TestStatus,
)

from .operator import DEFAULT_OPERATORS, Mutant, OperatorId, generate_mutants


OPERATOR_SET_VERSION = "v1"


class MutantOutcome(str, Enum):
    KILLED_ASSERTION = "killed_assertion"
    KILLED_EXCEPTION = "killed_exception"
    KILLED_TIMEOUT = "killed_timeout"
    SURVIVED = "survived"
    INVALID = "invalid"


class MutantResult(BaseModel):
    mutant_id: str
    operator_id: OperatorId
    file: str
    line: int
    status: MutantOutcome
    killed_by: str | None = None


class MutationTrace(BaseModel):
    started_at: str
    elapsed_ms: int
    operator_ids: list[str]
    mutant_generation_digest: str
    killed_by_assertion: int = 0
    killed_by_exception: int = 0
    killed_by_timeout: int = 0
    warnings: list[str] = Field(default_factory=list)


class MutationScoreReport(BaseModel):
    target_symbol: str
    test_id: str
    operator_set_version: str = OPERATOR_SET_VERSION
    seed: int = 0
    mutants_total: int
    mutations_run: int
    mutations_killed: int
    kill_rate: float
    killed_by_assertion: int
    killed_by_exception: int
    killed_by_timeout: int
    survived: int
    mutant_ids: list[str]
    equivalent_mutants_undetected: bool = True
    mutant_results: list[MutantResult] = Field(default_factory=list)
    trace: MutationTrace


def _test_id_from_path(test_path: Path, root: Path) -> str:
    relative = test_path.relative_to(root).as_posix()
    try:
        tree = ast.parse(test_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return relative
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            return f"{relative}::{node.name}"
    return relative


def _classify(outcome: TestOutcome, test_id: str) -> tuple[MutantOutcome, str | None]:
    if outcome.status in (TestStatus.PASSED, TestStatus.SKIPPED):
        return MutantOutcome.SURVIVED, None
    if outcome.status == TestStatus.TIMEOUT:
        return MutantOutcome.KILLED_TIMEOUT, test_id
    if outcome.status == TestStatus.FAILED:
        if outcome.failure_kind == FailureKind.ASSERTION:
            return MutantOutcome.KILLED_ASSERTION, test_id
        if outcome.failure_kind in (
            FailureKind.EXCEPTION,
            FailureKind.IMPORT_ERROR,
            FailureKind.COLLECTION_ERROR,
        ):
            return MutantOutcome.KILLED_EXCEPTION, test_id
    return MutantOutcome.INVALID, None


def _run_mutant(
    profile: ProjectProfile,
    mutant: Mutant,
    relative_target: Path,
    relative_test: Path,
    test_id: str,
    config: SandboxConfig,
) -> tuple[TestOutcome | None, str | None]:
    with tempfile.TemporaryDirectory(prefix="testlens_mutant_") as temporary:
        temporary_root = Path(temporary)
        project_root = temporary_root / "project"
        shutil.copytree(
            profile.root,
            project_root,
            ignore=shutil.ignore_patterns(
                "__pycache__",
                ".pytest_cache",
                "*.pyc",
                ".git",
                ".venv",
            ),
        )
        mutated_file = project_root / relative_target
        mutated_file.write_text(mutant.mutated_source, encoding="utf-8")

        mutated_profile = profile.model_copy(
            update={
                "root": project_root,
                "test_paths": [project_root / relative_test],
                "target_symbols": [],
            }
        )
        mutant_config = config.model_copy(update={"double_run": False})
        attempt = runner.run_once(
            mutated_profile,
            mutant_config,
            run_index=0,
            artifact_dir=temporary_root / "artifacts",
        )
        if not attempt.result.tests:
            messages = [error.message for error in attempt.errors]
            detail = f": {'; '.join(messages)}" if messages else ""
            return None, f"mutant produced no test result{detail}"
        outcome = next(
            (item for item in attempt.result.tests if item.test_id == test_id),
            attempt.result.tests[0],
        )
        if outcome.test_id != test_id:
            return None, (
                f"mutant result test id {outcome.test_id!r} "
                f"did not match expected {test_id!r}"
            )
        return outcome, None


def score_test(
    profile: ProjectProfile,
    target: TargetSymbol,
    test_path: Path,
    *,
    operators: tuple[OperatorId, ...] = DEFAULT_OPERATORS,
    sandbox_config: SandboxConfig | None = None,
    seed: int = 0,
) -> MutationScoreReport:
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    profile = runner.resolve_profile(profile)
    target_file = target.file if target.file.is_absolute() else profile.root / target.file
    target = target.model_copy(update={"file": target_file.resolve()})
    test_path = Path(test_path)
    if not test_path.is_absolute():
        test_path = profile.root / test_path
    test_path = test_path.resolve()
    relative_test = test_path.relative_to(profile.root)
    relative_target = target.file.relative_to(profile.root)
    test_id = _test_id_from_path(test_path, profile.root)

    config = runner.resolve_config(
        sandbox_config or SandboxConfig(double_run=False)
    ).model_copy(update={"double_run": False})

    mutants = generate_mutants(
        target.file,
        target,
        operators=operators,
        seed=seed,
    )
    mutant_ids = [mutant.mutant_id for mutant in mutants]
    generation_digest = hashlib.sha256(
        "\n".join(mutant_ids).encode("utf-8")
    ).hexdigest()[:16]

    results: list[MutantResult] = []
    warnings: list[str] = []
    killed_assertion = 0
    killed_exception = 0
    killed_timeout = 0
    survived = 0
    compiled_count = 0

    for mutant in mutants:
        try:
            compile(mutant.mutated_source, str(mutant.file), "exec")
        except SyntaxError as exc:
            results.append(
                MutantResult(
                    mutant_id=mutant.mutant_id,
                    operator_id=mutant.operator_id,
                    file=str(mutant.file),
                    line=mutant.line,
                    status=MutantOutcome.INVALID,
                )
            )
            warnings.append(f"{mutant.mutant_id}: mutated source does not compile: {exc}")
            continue

        compiled_count += 1
        outcome, error = _run_mutant(
            profile,
            mutant,
            relative_target,
            relative_test,
            test_id,
            config,
        )
        if outcome is None:
            results.append(
                MutantResult(
                    mutant_id=mutant.mutant_id,
                    operator_id=mutant.operator_id,
                    file=str(mutant.file),
                    line=mutant.line,
                    status=MutantOutcome.INVALID,
                )
            )
            if error:
                warnings.append(f"{mutant.mutant_id}: {error}")
            continue

        status, killed_by = _classify(outcome, test_id)
        if status == MutantOutcome.INVALID:
            results.append(
                MutantResult(
                    mutant_id=mutant.mutant_id,
                    operator_id=mutant.operator_id,
                    file=str(mutant.file),
                    line=mutant.line,
                    status=status,
                )
            )
            warnings.append(
                f"{mutant.mutant_id}: unclassified outcome "
                f"{outcome.status.value}/{outcome.failure_kind.value}"
            )
            continue

        if status == MutantOutcome.KILLED_ASSERTION:
            killed_assertion += 1
        elif status == MutantOutcome.KILLED_EXCEPTION:
            killed_exception += 1
        elif status == MutantOutcome.KILLED_TIMEOUT:
            killed_timeout += 1
        elif status == MutantOutcome.SURVIVED:
            survived += 1

        results.append(
            MutantResult(
                mutant_id=mutant.mutant_id,
                operator_id=mutant.operator_id,
                file=str(mutant.file),
                line=mutant.line,
                status=status,
                killed_by=killed_by,
            )
        )

    mutations_killed = killed_assertion + killed_exception + killed_timeout
    unusable = compiled_count - mutations_killed - survived
    if unusable:
        warnings.append(
            f"{unusable} compiled mutants produced no usable outcome; "
            "they remain in the kill-rate denominator"
        )
    kill_rate = mutations_killed / compiled_count if compiled_count else 0.0
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    return MutationScoreReport(
        target_symbol=target.symbol_id,
        test_id=test_id,
        operator_set_version=OPERATOR_SET_VERSION,
        seed=seed,
        mutants_total=len(mutants),
        mutations_run=compiled_count,
        mutations_killed=mutations_killed,
        kill_rate=kill_rate,
        killed_by_assertion=killed_assertion,
        killed_by_exception=killed_exception,
        killed_by_timeout=killed_timeout,
        survived=survived,
        mutant_ids=mutant_ids,
        equivalent_mutants_undetected=True,
        mutant_results=results,
        trace=MutationTrace(
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            operator_ids=[operator.value for operator in operators],
            mutant_generation_digest=generation_digest,
            killed_by_assertion=killed_assertion,
            killed_by_exception=killed_exception,
            killed_by_timeout=killed_timeout,
            warnings=sorted(set(warnings)),
        ),
    )
