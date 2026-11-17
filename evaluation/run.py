"""Evaluation harness: run both pipelines on the vendored fixtures.

One invocation produces a timestamped report under ``eval/reports/``. The
fixture proposals use the deterministic fake model, double-run is mandatory,
and the report records the platform, enforcement mode, threshold, seed, and
the equivalent-mutant caveat.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from audit.grade import grade_and_emit
from audit.independent import scan_independent
from audit.repro import run_repro
from audit.rule_scan import scan_rules
from audit.triage import llm_triage
from generation.generate import (
    GENERATION_TEMPLATE_VERSION,
    SandboxUnavailableError,
    generate_tests,
)
from llm.base import LLMClient
from llm.fake import FAKE_MODEL_ID, DeterministicFakeLLM
from llm.models import LLMCallStats, ModelKind
from pipeline.models import TestReport
from pipeline.run import run_test_pipeline
from review.cross_check import (
    cross_check,
    subjects_from_candidates,
    subjects_from_independent_claims,
)
from review.fake import DeterministicReviewer
from review.models import (
    EvidenceKind,
    EvidenceRole,
    ReviewerVerdict,
    ReviewVerdict,
)
from review.source import load_review_source, make_evidence
from sandbox import runner
from sandbox.health import SandboxHealth, check_sandbox_ready
from sandbox.runner import ProjectProfile, SandboxConfig
from targets.selection import select_targets

from .labels import (
    FixtureError,
    fixture_roots,
    load_independent_proposals,
    load_labels,
    load_proposals,
)
from .metrics import calibration_rows, summarize_audit_side, summarize_test_side
from .models import (
    CalibrationRow,
    EvaluationLabels,
    EvaluationReport,
    EvaluationTrace,
    FixtureEvaluation,
)

DEFAULT_FIXTURES = Path(__file__).resolve().parent / "fixtures"
DEFAULT_CALIBRATION = (0.2, 0.5, 0.8)


def default_reports_root() -> Path:
    """Reports are written under the current working directory, not the package."""
    return Path.cwd() / "eval" / "reports"


def _fixture_reviewer(
    root: Path,
    labels: EvaluationLabels,
    independent,
) -> DeterministicReviewer:
    """Register explicit fixture verdicts for independent semantic claims.

    Unmatched claims stay unregistered and are therefore retracted as
    unverifiable by the deterministic reviewer. This keeps the fake reviewer
    from auto-passing semantic claims while still exercising the full
    cross-check and grading path offline.
    """
    if independent is None:
        return DeterministicReviewer()
    reviews = {
        label.file: load_review_source(root, label.file)[0]
        for label in [
            *labels.defects,
            *labels.false_positives,
        ]
        if label.source == "llm_independent"
    }
    verdicts: dict[str, ReviewerVerdict] = {}
    defect_labels = [
        label
        for label in labels.defects
        if label.source == "llm_independent"
    ]
    false_positive_labels = [
        label
        for label in labels.false_positives
        if label.source == "llm_independent"
    ]
    for claim in independent.claims:
        source = reviews.get(claim.file)
        if source is None:
            continue

        def matches(label) -> bool:
            return (
                label.file == claim.file
                and label.claim_kind == claim.claim_kind.value
                and claim.line_start <= label.line <= claim.line_end
            )

        if any(matches(label) for label in defect_labels):
            evidence = make_evidence(
                source,
                claim.line_start,
                claim.line_end,
                kind=EvidenceKind.DATA_FLOW,
                role=EvidenceRole.SUPPORTS,
                explanation="fixture label marks this semantic defect",
            )
            verdicts[claim.finding_id] = ReviewerVerdict(
                verdict=ReviewVerdict.PASS, evidence_lines=[evidence]
            )
        elif any(matches(label) for label in false_positive_labels):
            evidence = make_evidence(
                source,
                claim.line_start,
                claim.line_end,
                kind=EvidenceKind.DATA_FLOW,
                role=EvidenceRole.REFUTES,
                explanation="fixture label marks this semantic claim as false",
            )
            verdicts[claim.finding_id] = ReviewerVerdict(
                verdict=ReviewVerdict.REFUTE, evidence_lines=[evidence]
            )
    return DeterministicReviewer(verdicts)


def _evaluate_fixture(
    root: Path,
    labels: EvaluationLabels,
    health: SandboxHealth,
    *,
    threshold: float,
    seed: int,
    config: SandboxConfig,
    warnings: set[str],
    llm_factory: Callable[[Mapping[str, str] | None], LLMClient],
    independent: bool = False,
) -> tuple[FixtureEvaluation, TestReport, LLMCallStats]:
    profile = ProjectProfile(
        profile_id=root.name,
        root=root,
        test_paths=[],
        target_symbols=[],
    )
    selection = select_targets(profile)
    responses = load_proposals(root)
    llm = llm_factory(responses)
    generation = generate_tests(
        profile,
        selection,
        llm,
        sandbox_health=health,
    )
    if generation.rejected:
        warnings.add(
            f"{root.name}: {len(generation.rejected)} proposals rejected during "
            "generation"
        )

    test_report = run_test_pipeline(
        profile,
        selection,
        generation,
        kill_threshold=threshold,
        seed=seed,
        sandbox_config=config,
    )
    if not test_report.trace.double_run:
        raise ValueError("evaluation reports require double_run=true")

    scan = scan_rules(profile)
    independent_report = (
        scan_independent(
            profile,
            scan,
            llm_factory(load_independent_proposals(root)),
        )
        if independent
        else None
    )
    if independent_report is not None and independent_report.rejected:
        warnings.add(
            f"{root.name}: {len(independent_report.rejected)} independent "
            "claims rejected"
        )
    triage = llm_triage(profile, scan, llm_factory(None))
    if triage.rejected:
        warnings.add(
            f"{root.name}: {len(triage.rejected)} triage drafts rejected"
        )
    subjects = subjects_from_candidates(scan, triage)
    if independent_report is not None:
        subjects = subjects + subjects_from_independent_claims(
            independent_report
        )
    review = cross_check(
        profile,
        scan,
        subjects,
        _fixture_reviewer(root, labels, independent_report),
    )
    with tempfile.TemporaryDirectory(
        prefix=f"testlens_eval_{root.name}_"
    ) as temporary:
        repro = run_repro(
            profile,
            scan,
            review,
            sandbox_config=config,
            artifact_root=Path(temporary),
        )
    audit_report = grade_and_emit(
        scan, triage, review, repro, independent_report=independent_report
    )

    if audit_report.folded_suspicious:
        warnings.add(
            f"{root.name}: {len(audit_report.folded_suspicious)} findings folded "
            "as SUSPICIOUS"
        )
    failed_repro = [
        artifact
        for artifact in repro.artifacts
        if not artifact.reproduced
    ]
    if failed_repro:
        warnings.add(
            f"{root.name}: {len(failed_repro)} reproduction attempts did not "
            "reproduce the claim"
        )

    generation_trace = generation.trace
    triage_trace = triage.trace
    usage = LLMCallStats(
        calls=generation_trace.llm_calls + triage_trace.llm_calls,
        retries=generation_trace.llm_retries + triage_trace.llm_retries,
        prompt_tokens=(
            generation_trace.prompt_tokens + triage_trace.prompt_tokens
        ),
        completion_tokens=(
            generation_trace.completion_tokens + triage_trace.completion_tokens
        ),
        replayed=generation_trace.replayed or triage_trace.replayed,
    )
    if independent_report is not None:
        independent_trace = independent_report.trace
        usage = LLMCallStats(
            calls=usage.calls + independent_trace.llm_calls,
            retries=usage.retries + independent_trace.llm_retries,
            prompt_tokens=usage.prompt_tokens + independent_trace.prompt_tokens,
            completion_tokens=(
                usage.completion_tokens + independent_trace.completion_tokens
            ),
            replayed=usage.replayed or independent_trace.replayed,
        )
    return (
        FixtureEvaluation(
            project=root.name,
            test=summarize_test_side(
                test_report, threshold=threshold, seed=seed
            ),
            audit=summarize_audit_side(
                audit_report,
                labels,
                independent=independent_report is not None,
            ),
        ),
        test_report,
        usage,
    )


def run_evaluation(
    fixtures: Sequence[Path | str] | None = None,
    *,
    output_root: Path | str | None = None,
    threshold: float = 0.5,
    seed: int = 0,
    write: bool = True,
    sandbox_config: SandboxConfig | None = None,
    health_timeout_seconds: float = 30.0,
    calibration_thresholds: Sequence[float] = DEFAULT_CALIBRATION,
    llm_factory: Callable[[Mapping[str, str] | None], LLMClient] | None = None,
    independent: bool = False,
) -> EvaluationReport:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    if any(not 0.0 <= value <= 1.0 for value in calibration_thresholds):
        raise ValueError("calibration thresholds must be between 0 and 1")

    factory = llm_factory or (
        lambda responses=None: DeterministicFakeLLM(responses)
    )
    probe = factory(None)
    model_kind = getattr(probe, "model_kind", ModelKind.FAKE)
    model_id = getattr(probe, "model_id", FAKE_MODEL_ID)
    replayed = bool(getattr(probe, "replayed", False))

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    config = runner.resolve_config(
        sandbox_config or SandboxConfig(double_run=True)
    )
    if not config.double_run:
        raise ValueError("evaluation requires double_run=true")

    roots = (
        [Path(path).expanduser().resolve() for path in fixtures]
        if fixtures is not None
        else fixture_roots(DEFAULT_FIXTURES)
    )
    if not roots:
        raise FixtureError("no fixtures provided")

    labels_by_root = {root: load_labels(root) for root in roots}
    if independent:
        for root in roots:
            labels = labels_by_root[root]
            if not any(
                label.source == "llm_independent" for label in labels.defects
            ):
                raise FixtureError(
                    f"{root.name}: --independent requires llm_independent labels"
                )

    health = check_sandbox_ready(
        timeout_seconds=health_timeout_seconds, config=config
    )
    if not health.ready:
        raise SandboxUnavailableError(health)

    warnings: set[str] = set()
    fixture_reports: list[FixtureEvaluation] = []
    calibration: list[CalibrationRow] = []
    usage = LLMCallStats()

    for root in roots:
        labels = labels_by_root[root]
        evaluation, test_report, fixture_usage = _evaluate_fixture(
            root,
            labels,
            health,
            threshold=threshold,
            seed=seed,
            config=config,
            warnings=warnings,
            llm_factory=factory,
            independent=independent,
        )
        fixture_reports.append(evaluation)
        usage = LLMCallStats(
            calls=usage.calls + fixture_usage.calls,
            retries=usage.retries + fixture_usage.retries,
            prompt_tokens=usage.prompt_tokens + fixture_usage.prompt_tokens,
            completion_tokens=(
                usage.completion_tokens + fixture_usage.completion_tokens
            ),
            replayed=usage.replayed or fixture_usage.replayed,
        )
        calibration.extend(
            calibration_rows(
                root.name, evaluation.test, calibration_thresholds
            )
        )

    trace = EvaluationTrace(
        started_at=started_at,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        fixtures=[root.name for root in roots],
        model_kind=model_kind,
        model_id=model_id,
        prompt_template_version=GENERATION_TEMPLATE_VERSION,
        replayed=replayed or usage.replayed,
        independent=independent,
        llm_calls=usage.calls,
        llm_retries=usage.retries,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        seed=seed,
        threshold=threshold,
        calibration_thresholds=list(calibration_thresholds),
        double_run=config.double_run,
        platform=config.platform,
        enforcement_mode=config.enforcement_mode,
        sandbox_backend=config.backend,
        sandbox_ready=health.ready,
        warnings=sorted(warnings),
    )
    report = EvaluationReport(
        fixtures=sorted(fixture_reports, key=lambda item: item.project),
        calibration=sorted(
            calibration,
            key=lambda item: (item.project, item.threshold),
        ),
        trace=trace,
    )
    if not write:
        return report
    root = (
        Path(output_root).expanduser().resolve()
        if output_root is not None
        else default_reports_root()
    )
    if model_kind is ModelKind.REAL:
        root = root / ("replay" if replayed else "real")
    return write_evaluation(report, root)


def render_summary(report: EvaluationReport) -> str:
    trace = report.trace
    lines = [
        "# TestLens evaluation",
        "",
        f"- started_at: `{trace.started_at}`",
        f"- elapsed_ms: {trace.elapsed_ms}",
        f"- model: `{trace.model_kind.value}` / `{trace.model_id}`",
        f"- replayed: {trace.replayed}",
        f"- independent: {trace.independent}",
        f"- model_calls: {trace.llm_calls} (retries: {trace.llm_retries})",
        f"- model_tokens: prompt={trace.prompt_tokens} "
        f"completion={trace.completion_tokens}",
        f"- prompt_template: `{trace.prompt_template_version}`",
        f"- threshold: {trace.threshold}",
        f"- seed: {trace.seed}",
        f"- double_run: {trace.double_run}",
        f"- platform: {trace.platform}",
        f"- sandbox_backend: `{trace.sandbox_backend.value}`",
        f"- enforcement_mode: {trace.enforcement_mode.value}",
        "",
    ]
    for fixture in report.fixtures:
        test = fixture.test
        audit = fixture.audit
        mean_kill = (
            "n/a"
            if test.mean_kill_rate is None
            else f"{test.mean_kill_rate:.2f}"
        )
        precision = (
            "n/a" if audit.precision is None else f"{audit.precision:.2f}"
        )
        lines.extend(
            [
                f"## {fixture.project}",
                "",
                "### Test side",
                "",
                "| candidates | kept | flaky | flaky_rate | mean_kill_rate | mutations_run | killed | assertion | exception | timeout |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                (
                    f"| {test.candidates} | {test.kept} | {test.flaky} | "
                    f"{test.flaky_rate:.2f} | {mean_kill} | "
                    f"{test.mutations_run} | {test.mutations_killed} | "
                    f"{test.killed_by_assertion} | {test.killed_by_exception} | "
                    f"{test.killed_by_timeout} |"
                ),
                "",
                "Discard reasons: "
                + (
                    ", ".join(
                        f"`{reason}`={count}"
                        for reason, count in test.discarded_by_reason.items()
                    )
                    or "none"
                ),
                "",
                "### Audit side",
                "",
                "| findings | labeled defects | found | precision | false positives | FP labels retracted | FN labels observed |",
                "|---:|---:|---:|---:|---:|---:|---:|",
                (
                    f"| {audit.findings_emitted} | {audit.defects_labeled} | "
                    f"{audit.defects_found} | {precision} | "
                    f"{audit.false_positives} | "
                    f"{audit.false_positives_retracted} | "
                    f"{audit.false_negatives_observed} |"
                ),
                "",
                "| rule | labeled | found | emitted | TP | FP |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        lines.extend(
            f"| `{rule.rule_id}` | {rule.labeled_defects} | {rule.defects_found} | "
            f"{rule.emitted_findings} | {rule.true_positives} | {rule.false_positives} |"
            for rule in audit.per_rule
        )
        if audit.independent_candidates or audit.independent_findings:
            independent_precision = (
                "n/a"
                if audit.independent_precision is None
                else f"{audit.independent_precision:.2f}"
            )
            independent_recall = (
                "n/a"
                if audit.independent_recall is None
                else f"{audit.independent_recall:.2f}"
            )
            lines.extend(
                [
                    "",
                    "Independent claims: "
                    f"candidates={audit.independent_candidates}, "
                    f"findings={audit.independent_findings}, "
                    f"precision={independent_precision}, "
                    f"recall={independent_recall}, "
                    f"retracted={audit.independent_retracted}",
                ]
            )
        lines.extend(
            [
                "",
                "Evidence levels: "
                + (
                    ", ".join(
                        f"`{level}`={count}"
                        for level, count in audit.evidence_level_counts.items()
                    )
                    or "none"
                ),
                "",
                "Retract reasons: "
                + (
                    ", ".join(
                        f"`{reason}`={count}"
                        for reason, count in audit.retract_reason_counts.items()
                    )
                    or "none"
                ),
                "",
            ]
        )

    lines.extend(
        [
            "## Calibration",
            "",
            "| project | threshold | kept | discard distribution |",
            "|---|---:|---:|---|",
        ]
    )
    for row in report.calibration:
        distribution = (
            ", ".join(
                f"{reason}={count}"
                for reason, count in row.discarded_by_reason.items()
            )
            or "none"
        )
        lines.append(
            f"| {row.project} | {row.threshold} | {row.kept} | {distribution} |"
        )

    if trace.model_kind is ModelKind.REAL:
        model_caveat = (
            "Model proposals come from a labeled real/replay run and are "
            "excluded from deterministic baseline comparisons."
        )
    else:
        model_caveat = (
            "Model proposals are deterministic fake responses from the fixture "
            "corpus, not real-model output."
        )
    lines.extend(
        [
            "",
            "Caveats: equivalent mutants are not detected, so the observed "
            f"kill-rate ceiling is depressed. {model_caveat}",
            "",
        ]
    )
    if trace.warnings:
        lines.extend(["Warnings:", ""])
        lines.extend(f"- {warning}" for warning in trace.warnings)
        lines.append("")
    return "\n".join(lines)


def write_evaluation(
    report: EvaluationReport, output_root: Path
) -> EvaluationReport:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = output_root / stamp
    output_dir.mkdir(parents=True, exist_ok=False)
    updated = report.model_copy(update={"output_dir": str(output_dir)})

    (output_dir / "report.json").write_text(
        json.dumps(updated.model_dump(mode="json"), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "calibration.json").write_text(
        json.dumps(
            [row.model_dump(mode="json") for row in updated.calibration],
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.md").write_text(
        render_summary(updated), encoding="utf-8"
    )
    return updated
