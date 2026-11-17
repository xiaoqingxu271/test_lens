"""Evidence grading and audit report emission.

Levels are recomputed here from the final evidence, never set by hand:

- ``REPRODUCED`` requires a successful sandbox repro artifact;
- ``STATIC_CONFIRMED`` requires rule confirmation or reviewer evidence lines;
- otherwise the finding is ``SUSPICIOUS`` and folded by default.

A refute or unverifiable verdict retracts the finding with an enumerated
``RetractReason``. Missing cross-check results are a pipeline error.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timezone

from review.models import (
    CrossCheckReport,
    EvidenceKind,
    EvidenceRole,
    ReviewerKind,
    ReviewVerdict,
)

from .models import (
    AuditReport,
    EvidenceLevel,
    Finding,
    FindingSource,
    GradingTrace,
    IndependentClaim,
    IndependentReport,
    ReproReport,
    Retracted,
    RetractReason,
    RuleCandidate,
    RuleScanReport,
    TriageDraft,
    TriageReport,
)


def _static_evidence(result) -> bool:
    supports = [
        line
        for line in result.evidence_lines
        if line.role == EvidenceRole.SUPPORTS
    ]
    if not supports:
        return False
    if result.checked_rule_ids and any(
        line.evidence_kind == EvidenceKind.RULE for line in supports
    ):
        return True
    return result.reviewer_kind in (ReviewerKind.FAKE, ReviewerKind.MODEL)


def _finding(
    candidate: RuleCandidate,
    draft: TriageDraft | None,
    artifact,
    level: EvidenceLevel,
) -> Finding:
    return Finding(
        finding_id=candidate.finding_id,
        file=candidate.relative_file,
        line_start=candidate.line_start,
        line_end=candidate.line_end,
        rule_id=candidate.rule_id,
        llm_finding_id=draft.finding_id if draft is not None else None,
        source=draft.source if draft is not None else FindingSource.RULE,
        title=candidate.title,
        evidence_level=level,
        claim=draft.claim if draft is not None else candidate.message,
        trigger_path=list(draft.trigger_path) if draft is not None else [],
        repro_artifact=artifact,
    )


def _independent_finding(
    claim: IndependentClaim, level: EvidenceLevel
) -> Finding:
    from .independent import claim_title

    return Finding(
        finding_id=claim.finding_id,
        file=claim.file,
        line_start=claim.line_start,
        line_end=claim.line_end,
        rule_id=None,
        llm_finding_id=None,
        source=FindingSource.LLM_INDEPENDENT,
        title=claim_title(claim),
        evidence_level=level,
        claim=claim.claim,
        claim_kind=claim.claim_kind,
        trigger_path=list(claim.trigger_path),
    )


def _sort_key(finding: Finding) -> tuple[str, int, int, str, str]:
    return (
        finding.file,
        finding.line_start,
        finding.line_end,
        finding.rule_id.value if finding.rule_id is not None else "",
        finding.finding_id,
    )


def grade_and_emit(
    rule_report: RuleScanReport,
    triage_report: TriageReport,
    cross_check_report: CrossCheckReport,
    repro_report: ReproReport,
    independent_report: IndependentReport | None = None,
) -> AuditReport:
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    drafts = {draft.finding_id: draft for draft in triage_report.drafts}
    results = {result.finding_id: result for result in cross_check_report.results}
    artifacts = {
        artifact.finding_id: artifact for artifact in repro_report.artifacts
    }

    if len(results) != len(cross_check_report.results):
        raise ValueError("duplicate cross-check results")
    if len(artifacts) != len(repro_report.artifacts):
        raise ValueError("duplicate repro artifacts")

    candidate_ids = {candidate.finding_id for candidate in rule_report.candidates}
    missing = candidate_ids - results.keys()
    if missing:
        raise ValueError(
            "missing cross-check results: " + ", ".join(sorted(missing))
        )

    independent_claims = (
        list(independent_report.claims) if independent_report is not None else []
    )
    independent_ids = {claim.finding_id for claim in independent_claims}
    if len(independent_ids) != len(independent_claims):
        raise ValueError("duplicate independent finding ids")
    missing_independent = independent_ids - results.keys()
    if missing_independent:
        raise ValueError(
            "missing independent cross-check results: "
            + ", ".join(sorted(missing_independent))
        )

    unexpected = results.keys() - candidate_ids - independent_ids
    if unexpected:
        raise ValueError(
            "cross-check results without candidates: "
            + ", ".join(sorted(unexpected))
        )
    unexpected_artifacts = artifacts.keys() - candidate_ids
    if unexpected_artifacts:
        raise ValueError(
            "repro artifacts without candidates: "
            + ", ".join(sorted(unexpected_artifacts))
        )

    findings: list[Finding] = []
    folded: list[Finding] = []
    retracted: list[Retracted] = []
    level_counts: Counter[EvidenceLevel] = Counter()
    retract_counts: Counter[RetractReason] = Counter()

    for candidate in rule_report.candidates:
        result = results[candidate.finding_id]
        draft = drafts.get(candidate.finding_id)
        artifact = artifacts.get(candidate.finding_id)

        if result.verdict == ReviewVerdict.REFUTE:
            reason = result.retract_reason or RetractReason.REFUTED_BY_CONTEXT
            finding = _finding(candidate, draft, artifact, EvidenceLevel.SUSPICIOUS)
            retracted.append(Retracted(finding=finding, reason=reason))
            retract_counts[reason] += 1
            continue

        if result.verdict == ReviewVerdict.UNVERIFIABLE:
            reason = result.retract_reason or RetractReason.UNVERIFIABLE_CLAIM
            finding = _finding(candidate, draft, artifact, EvidenceLevel.SUSPICIOUS)
            retracted.append(Retracted(finding=finding, reason=reason))
            retract_counts[reason] += 1
            continue

        if artifact is not None and artifact.reproduced:
            level = EvidenceLevel.REPRODUCED
        elif _static_evidence(result):
            level = EvidenceLevel.STATIC_CONFIRMED
        else:
            level = EvidenceLevel.SUSPICIOUS

        finding = _finding(candidate, draft, artifact, level)
        level_counts[level] += 1
        if level == EvidenceLevel.SUSPICIOUS:
            folded.append(finding)
        else:
            findings.append(finding)

    independent_emitted = 0
    independent_retracted = 0
    for claim in independent_claims:
        result = results[claim.finding_id]
        if result.verdict == ReviewVerdict.REFUTE:
            reason = result.retract_reason or RetractReason.REFUTED_BY_CONTEXT
            retracted.append(
                Retracted(
                    finding=_independent_finding(
                        claim, EvidenceLevel.SUSPICIOUS
                    ),
                    reason=reason,
                )
            )
            retract_counts[reason] += 1
            independent_retracted += 1
            continue
        if result.verdict == ReviewVerdict.UNVERIFIABLE:
            reason = result.retract_reason or RetractReason.UNVERIFIABLE_CLAIM
            retracted.append(
                Retracted(
                    finding=_independent_finding(
                        claim, EvidenceLevel.SUSPICIOUS
                    ),
                    reason=reason,
                )
            )
            retract_counts[reason] += 1
            independent_retracted += 1
            continue

        # A semantic claim never folds as SUSPICIOUS: it either has structured
        # evidence and is statically confirmed, or it is retracted.
        if not _static_evidence(result):
            reason = RetractReason.UNVERIFIABLE_CLAIM
            retracted.append(
                Retracted(
                    finding=_independent_finding(
                        claim, EvidenceLevel.SUSPICIOUS
                    ),
                    reason=reason,
                )
            )
            retract_counts[reason] += 1
            independent_retracted += 1
            continue

        finding = _independent_finding(claim, EvidenceLevel.STATIC_CONFIRMED)
        level_counts[EvidenceLevel.STATIC_CONFIRMED] += 1
        findings.append(finding)
        independent_emitted += 1

    findings.sort(key=_sort_key)
    folded.sort(key=_sort_key)
    retracted.sort(
        key=lambda item: _sort_key(item.finding) + (item.reason.value,)
    )

    trace = GradingTrace(
        started_at=started_at,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        candidates_considered=len(rule_report.candidates),
        findings_emitted=len(findings),
        folded_suspicious=len(folded),
        retracted=len(retracted),
        evidence_level_counts=dict(
            sorted(level_counts.items(), key=lambda item: item[0].value)
        ),
        retract_reason_counts=dict(
            sorted(retract_counts.items(), key=lambda item: item[0].value)
        ),
        repro_attempted=len(repro_report.artifacts),
        repro_reproduced=sum(
            1 for artifact in repro_report.artifacts if artifact.reproduced
        ),
        independent_candidates_considered=len(independent_claims),
        independent_findings_emitted=independent_emitted,
        independent_retracted=independent_retracted,
        warnings=[],
    )
    return AuditReport(
        findings=findings,
        folded_suspicious=folded,
        retracted=retracted,
        trace=trace,
    )
