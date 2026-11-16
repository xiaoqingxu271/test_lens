"""Pure metric computation for evaluation reports."""

from __future__ import annotations

from collections.abc import Sequence

from audit.models import AuditReport, Finding, Retracted
from mutation.score import OPERATOR_SET_VERSION
from pipeline.models import DiscardReason, TestReport

from .models import (
    AuditSideEvaluation,
    CalibrationRow,
    EvaluationLabels,
    FindingSummary,
    Label,
    RuleEvaluation,
    TestSideEvaluation,
)


def _finding_matches(finding: Finding, label: Label) -> bool:
    if finding.file != label.file:
        return False
    if finding.rule_id is None or finding.rule_id.value != label.rule_id:
        return False
    return finding.line_start <= label.line <= finding.line_end


def _retracted_matches(item: Retracted, label: Label) -> bool:
    if item.finding.file != label.file:
        return False
    if (
        item.finding.rule_id is None
        or item.finding.rule_id.value != label.rule_id
    ):
        return False
    if not (item.finding.line_start <= label.line <= item.finding.line_end):
        return False
    if label.retract_reason is None:
        return True
    return item.reason.value == label.retract_reason


def summarize_test_side(
    report: TestReport,
    *,
    threshold: float,
    seed: int,
) -> TestSideEvaluation:
    trace = report.trace
    scored = [
        item.mutation_kill_rate
        for item in [*report.kept, *report.discarded]
        if item.mutation_kill_rate is not None
    ]
    kept_rates = [item.mutation_kill_rate for item in report.kept]
    discarded = {
        reason.value: count
        for reason, count in trace.discarded_by_reason.items()
    }
    unscored = {
        reason: count
        for reason, count in discarded.items()
        if reason != DiscardReason.ASSERTION_TOO_WEAK.value
    }
    candidates = trace.candidates_in
    return TestSideEvaluation(
        candidates=candidates,
        kept=len(report.kept),
        discarded_by_reason=discarded,
        flaky=len(report.flaky),
        flaky_rate=(len(report.flaky) / candidates if candidates else 0.0),
        mutation_scored=trace.mutation_scored,
        mutations_run=trace.mutations_run,
        mutations_killed=trace.mutations_killed,
        killed_by_assertion=trace.killed_by_assertion,
        killed_by_exception=trace.killed_by_exception,
        killed_by_timeout=trace.killed_by_timeout,
        mean_kill_rate=(
            sum(kept_rates) / len(kept_rates) if kept_rates else None
        ),
        mean_kill_rate_scored=(
            trace.mutations_killed / trace.mutations_run
            if trace.mutations_run
            else None
        ),
        mutation_kill_rates=sorted(scored),
        unscored_discarded_by_reason=dict(sorted(unscored.items())),
        double_run=trace.double_run,
        platform=trace.platform,
        enforcement_mode=trace.enforcement_mode,
        seed=seed,
        threshold=threshold,
        operator_set_version=OPERATOR_SET_VERSION,
        equivalent_mutants_undetected=True,
    )


def summarize_audit_side(
    report: AuditReport, labels: EvaluationLabels
) -> AuditSideEvaluation:
    findings = report.findings
    matched = [any(_finding_matches(item, label) for label in labels.defects) for item in findings]
    true_positives = sum(1 for value in matched if value)
    false_positives = len(findings) - true_positives

    defects_found = sum(
        1
        for label in labels.defects
        if any(_finding_matches(item, label) for item in findings)
    )
    false_positives_retracted = sum(
        1
        for label in labels.false_positives
        if any(_retracted_matches(item, label) for item in report.retracted)
    )
    false_negatives_observed = sum(
        1
        for label in labels.false_negatives
        if any(_finding_matches(item, label) for item in findings)
    )

    rules = sorted(
        {
            label.rule_id
            for label in [
                *labels.defects,
                *labels.false_positives,
                *labels.false_negatives,
            ]
        }
    )
    per_rule: list[RuleEvaluation] = []
    for rule_id in rules:
        rule_findings = [
            item
            for item in findings
            if item.rule_id is not None and item.rule_id.value == rule_id
        ]
        rule_labels = [
            label for label in labels.defects if label.rule_id == rule_id
        ]
        rule_tp = sum(
            1
            for item in rule_findings
            if any(_finding_matches(item, label) for label in rule_labels)
        )
        per_rule.append(
            RuleEvaluation(
                rule_id=rule_id,
                labeled_defects=len(rule_labels),
                defects_found=sum(
                    1
                    for label in rule_labels
                    if any(_finding_matches(item, label) for item in findings)
                ),
                emitted_findings=len(rule_findings),
                true_positives=rule_tp,
                false_positives=len(rule_findings) - rule_tp,
            )
        )

    summaries = [
        FindingSummary(
            file=item.file,
            line_start=item.line_start,
            line_end=item.line_end,
            rule_id=item.rule_id.value if item.rule_id is not None else None,
            source=item.source.value,
            evidence_level=item.evidence_level.value,
        )
        for item in findings
    ]
    summaries.sort(
        key=lambda item: (
            item.file,
            item.line_start,
            item.line_end,
            item.rule_id or "",
        )
    )

    return AuditSideEvaluation(
        candidates=report.trace.candidates_considered,
        findings_emitted=len(findings),
        defects_labeled=len(labels.defects),
        defects_found=defects_found,
        recall=(
            defects_found / len(labels.defects) if labels.defects else None
        ),
        true_positives=true_positives,
        false_positives=false_positives,
        precision=(true_positives / len(findings) if findings else None),
        false_positive_labels=len(labels.false_positives),
        false_positives_retracted=false_positives_retracted,
        false_negative_labels=len(labels.false_negatives),
        false_negatives_observed=false_negatives_observed,
        evidence_level_counts={
            level.value: count
            for level, count in report.trace.evidence_level_counts.items()
        },
        retract_reason_counts={
            reason.value: count
            for reason, count in report.trace.retract_reason_counts.items()
        },
        per_rule=per_rule,
        findings=summaries,
    )


def calibration_rows(
    project: str,
    summary: TestSideEvaluation,
    thresholds: Sequence[float],
) -> list[CalibrationRow]:
    rates = summary.mutation_kill_rates
    rows: list[CalibrationRow] = []
    for threshold in thresholds:
        weak = sum(1 for rate in rates if rate < threshold)
        discarded = dict(summary.unscored_discarded_by_reason)
        if weak:
            discarded[DiscardReason.ASSERTION_TOO_WEAK.value] = weak
        rows.append(
            CalibrationRow(
                project=project,
                threshold=threshold,
                kept=len(rates) - weak,
                discarded_by_reason=dict(sorted(discarded.items())),
            )
        )
    return rows
