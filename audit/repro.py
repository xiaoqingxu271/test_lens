"""Sandbox reproduction for rule claims.

Only cross-check ``pass`` claims are attempted, and only rules with an
enumerated template. v1 ships the R001 mutable-default template: a minimal
pytest test mutates the object behind the first default-backed call and
asserts the mutation is visible on a second call. The test runs in an
isolated work directory through the normal double-run executor; the target
project is never written to.

``reproduced=true`` is the only path to ``REPRODUCED`` at grading time.
"""

from __future__ import annotations

import ast
import hashlib
import tempfile
import time
import tokenize
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from review.models import CrossCheckReport, ReviewVerdict
from sandbox import double_run
from sandbox.runner import (
    ExecutionReport,
    FailureKind,
    FinalStatus,
    ProjectProfile,
    SandboxConfig,
)

from .models import (
    ReproArtifact,
    ReproFailureReason,
    ReproReport,
    ReproSkipReason,
    ReproSkipped,
    ReproTrace,
    RuleCandidate,
    RuleId,
    RuleScanReport,
)

SUPPORTED_REPRO_RULES = frozenset({RuleId.R001_MUTABLE_DEFAULT})
PROBE_VALUE = "testlens_probe"


@dataclass(frozen=True)
class Probe:
    mutate: str
    observe: str


def _module_name(relative_file: str) -> str:
    path = Path(relative_file)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _load_source(candidate: RuleCandidate) -> tuple[str | None, ast.Module | None, str | None]:
    path = Path(candidate.file)
    try:
        with tokenize.open(path) as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    try:
        tree = ast.parse(text, filename=str(path))
    except (SyntaxError, ValueError) as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    return text, tree, None


def _find_module_function(
    tree: ast.Module, candidate: RuleCandidate
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        if not (node.lineno <= candidate.line_start and candidate.line_end <= end):
            continue
        if best is None or (end - node.lineno) < (
            (best.end_lineno or best.lineno) - best.lineno
        ):
            best = node
    if best is None or best not in tree.body:
        return None
    return best


def _required_parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    required_count = len(positional) - len(args.defaults)
    required = [argument.arg for argument in positional[:required_count]]
    required.extend(
        argument.arg
        for argument, default in zip(args.kwonlyargs, args.kw_defaults)
        if default is None
    )
    return required


def _kind_of(node: ast.AST) -> str | None:
    if isinstance(node, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(node, (ast.Dict, ast.DictComp)):
        return "dict"
    if isinstance(node, (ast.Set, ast.SetComp)):
        return "set"
    if isinstance(node, ast.Call):
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "builtins"
        ):
            name = node.func.attr
        if name in {"list", "dict", "set", "bytearray"}:
            return name
    return None


def _probe_for(node: ast.AST, first: str = "first", second: str = "second") -> Probe | None:
    if isinstance(node, ast.Tuple):
        for index, element in enumerate(node.elts):
            probe = _probe_for(element, f"{first}[{index}]", f"{second}[{index}]")
            if probe is not None:
                return probe
        return None

    kind = _kind_of(node)
    if kind == "list":
        return Probe(f'{first}.append("{PROBE_VALUE}")', f'"{PROBE_VALUE}" in {second}')
    if kind == "dict":
        return Probe(f'{first}["{PROBE_VALUE}"] = True', f'"{PROBE_VALUE}" in {second}')
    if kind == "set":
        return Probe(f'{first}.add("{PROBE_VALUE}")', f'"{PROBE_VALUE}" in {second}')
    if kind == "bytearray":
        return Probe(
            f'{first}.extend(b"{PROBE_VALUE}")',
            f'b"{PROBE_VALUE}" in {second}',
        )
    return None


def _function_probe(function: ast.FunctionDef | ast.AsyncFunctionDef) -> Probe | None:
    for default in function.args.defaults:
        probe = _probe_for(default)
        if probe is not None:
            return probe
    for default in function.args.kw_defaults:
        if default is None:
            continue
        probe = _probe_for(default)
        if probe is not None:
            return probe
    return None


def _build_script(
    module: str,
    function_name: str,
    probe: Probe,
    finding_id: str,
) -> str:
    return (
        f"from {module} import {function_name}\n\n\n"
        f"def test_repro_{finding_id}():\n"
        f"    first = {function_name}()\n"
        f"    {probe.mutate}\n"
        f"    second = {function_name}()\n"
        f"    assert {probe.observe}\n"
    )


def _build_template(
    candidate: RuleCandidate,
    tree: ast.Module,
) -> tuple[str | None, ReproSkipReason | None, str | None]:
    function = _find_module_function(tree, candidate)
    if function is None:
        return (
            None,
            ReproSkipReason.TEMPLATE_BUILD_FAILED,
            "candidate is not inside a module-level function",
        )
    if isinstance(function, ast.AsyncFunctionDef):
        return (
            None,
            ReproSkipReason.UNSUPPORTED_SIGNATURE,
            "async functions are not reproduced in v1",
        )
    required = _required_parameters(function)
    if required:
        return (
            None,
            ReproSkipReason.UNSUPPORTED_SIGNATURE,
            "required parameters without defaults: " + ", ".join(required),
        )
    probe = _function_probe(function)
    if probe is None:
        return (
            None,
            ReproSkipReason.TEMPLATE_BUILD_FAILED,
            "no supported mutable-default probe",
        )
    script = _build_script(
        _module_name(candidate.relative_file),
        function.name,
        probe,
        candidate.finding_id,
    )
    return script, None, None


def classify_repro(
    report: ExecutionReport,
) -> tuple[bool, ReproFailureReason | None]:
    """Map an execution report to (reproduced, failure reason)."""
    if any(error.fatal for error in report.runner_errors):
        return False, ReproFailureReason.SANDBOX_ERROR
    if len(report.tests) != 1:
        return False, ReproFailureReason.SANDBOX_ERROR

    test = report.tests[0]
    if test.final_status == FinalStatus.FLAKY:
        return False, ReproFailureReason.FLAKY
    if test.final_status == FinalStatus.PASSED:
        return True, None
    if test.final_status == FinalStatus.TIMEOUT:
        return False, ReproFailureReason.TIMEOUT
    if (
        test.final_status == FinalStatus.FAILED
        and FailureKind.ASSERTION in test.failure_kinds
    ):
        return False, ReproFailureReason.CLAIM_NOT_OBSERVED
    return False, ReproFailureReason.SANDBOX_ERROR


def _artifact_digest(path: str | None) -> str | None:
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def run_repro(
    profile: ProjectProfile,
    rule_report: RuleScanReport,
    cross_check_report: CrossCheckReport,
    *,
    sandbox_config: SandboxConfig | None = None,
    artifact_root: Path | None = None,
) -> ReproReport:
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    root = Path(profile.root).expanduser().resolve()
    config = sandbox_config or SandboxConfig(double_run=True)
    artifacts_dir = (
        Path(artifact_root)
        if artifact_root is not None
        else Path(tempfile.mkdtemp(prefix="testlens_repro_"))
    )

    results = {result.finding_id: result for result in cross_check_report.results}
    artifacts: list[ReproArtifact] = []
    skipped: list[ReproSkipped] = []
    skip_counts: Counter[ReproSkipReason] = Counter()
    failure_counts: Counter[ReproFailureReason] = Counter()
    warnings: set[str] = set()

    for candidate in rule_report.candidates:
        result = results.get(candidate.finding_id)
        if result is None:
            warnings.add(
                f"missing cross-check result for {candidate.finding_id}"
            )
            skipped.append(
                ReproSkipped(
                    finding_id=candidate.finding_id,
                    rule_id=candidate.rule_id,
                    file=candidate.relative_file,
                    line_start=candidate.line_start,
                    line_end=candidate.line_end,
                    reason=ReproSkipReason.NOT_PASSED,
                    detail="no cross-check result",
                )
            )
            skip_counts[ReproSkipReason.NOT_PASSED] += 1
            continue

        if result.verdict != ReviewVerdict.PASS:
            skipped.append(
                ReproSkipped(
                    finding_id=candidate.finding_id,
                    rule_id=candidate.rule_id,
                    file=candidate.relative_file,
                    line_start=candidate.line_start,
                    line_end=candidate.line_end,
                    reason=ReproSkipReason.NOT_PASSED,
                    detail=f"cross-check verdict: {result.verdict.value}",
                )
            )
            skip_counts[ReproSkipReason.NOT_PASSED] += 1
            continue

        if candidate.rule_id not in SUPPORTED_REPRO_RULES:
            skipped.append(
                ReproSkipped(
                    finding_id=candidate.finding_id,
                    rule_id=candidate.rule_id,
                    file=candidate.relative_file,
                    line_start=candidate.line_start,
                    line_end=candidate.line_end,
                    reason=ReproSkipReason.UNSUPPORTED_RULE,
                    detail=f"no repro template for {candidate.rule_id.value}",
                )
            )
            skip_counts[ReproSkipReason.UNSUPPORTED_RULE] += 1
            continue

        _text, tree, read_error = _load_source(candidate)
        if tree is None:
            skipped.append(
                ReproSkipped(
                    finding_id=candidate.finding_id,
                    rule_id=candidate.rule_id,
                    file=candidate.relative_file,
                    line_start=candidate.line_start,
                    line_end=candidate.line_end,
                    reason=ReproSkipReason.TEMPLATE_BUILD_FAILED,
                    detail=read_error,
                )
            )
            skip_counts[ReproSkipReason.TEMPLATE_BUILD_FAILED] += 1
            continue

        script, skip_reason, detail = _build_template(candidate, tree)
        if script is None:
            assert skip_reason is not None
            skipped.append(
                ReproSkipped(
                    finding_id=candidate.finding_id,
                    rule_id=candidate.rule_id,
                    file=candidate.relative_file,
                    line_start=candidate.line_start,
                    line_end=candidate.line_end,
                    reason=skip_reason,
                    detail=detail,
                )
            )
            skip_counts[skip_reason] += 1
            continue

        script_dir = artifacts_dir / candidate.finding_id
        script_dir.mkdir(parents=True, exist_ok=True)
        script_path = script_dir / f"test_repro_{candidate.finding_id}.py"
        script_path.write_text(script, encoding="utf-8")

        repro_profile = ProjectProfile(
            profile_id=f"repro-{candidate.finding_id}",
            root=root,
            test_paths=[script_path],
            target_symbols=[],
            python_executable=profile.python_executable,
            env=dict(profile.env),
        )
        report = double_run.execute(
            repro_profile,
            config,
            artifact_root=script_dir,
        )
        reproduced, failure = classify_repro(report)
        if failure is not None:
            failure_counts[failure] += 1

        first_run = report.runs[0] if report.runs else None
        artifacts.append(
            ReproArtifact(
                finding_id=candidate.finding_id,
                rule_id=candidate.rule_id,
                file=candidate.relative_file,
                line_start=candidate.line_start,
                line_end=candidate.line_end,
                script_source=script,
                script_path=str(script_path),
                exit_code=first_run.exit_code if first_run is not None else -1,
                timed_out=report.timeout_summary.any_timeout,
                elapsed_ms=report.elapsed_ms,
                stdout_digest=(
                    _artifact_digest(first_run.stdout_artifact)
                    if first_run is not None
                    else None
                ),
                stderr_digest=(
                    _artifact_digest(first_run.stderr_artifact)
                    if first_run is not None
                    else None
                ),
                platform=report.sandbox_config.platform,
                enforcement_mode=report.sandbox_config.enforcement_mode,
                double_run=report.sandbox_config.double_run,
                reproduced=reproduced,
                failure_reason=failure,
            )
        )

    artifacts.sort(
        key=lambda artifact: (
            artifact.file,
            artifact.line_start,
            artifact.finding_id,
        )
    )
    skipped.sort(
        key=lambda item: (
            item.file,
            item.line_start,
            item.finding_id,
            item.reason.value,
        )
    )

    trace = ReproTrace(
        started_at=started_at,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        findings_considered=len(rule_report.candidates),
        attempted=len(artifacts),
        reproduced=sum(1 for artifact in artifacts if artifact.reproduced),
        skipped_by_reason=dict(
            sorted(skip_counts.items(), key=lambda item: item[0].value)
        ),
        failures_by_reason=dict(
            sorted(failure_counts.items(), key=lambda item: item[0].value)
        ),
        warnings=sorted(warnings),
    )
    return ReproReport(artifacts=artifacts, skipped=skipped, trace=trace)
