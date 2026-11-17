"""False-positive cross-check stage.

Rule-backed claims go to the deterministic rule reviewer first; its verdict is
authoritative. Semantic claims (``rule_id=None``) go to the independent
reviewer through a structured interface. Every returned evidence line is
verified against the current source before it can support ``pass`` or
``refute``; missing or unlocatable evidence downgrades the result.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from audit.models import (
    FindingSource,
    IndependentReport,
    RuleScanReport,
    TriageDraft,
    TriageReport,
)
from sandbox.runner import ProjectProfile

from .base import ClaimReviewer
from .context import build_review_context, build_review_request
from .models import (
    CrossCheckReport,
    CrossCheckResult,
    CrossCheckSubject,
    CrossCheckTrace,
    EvidenceRole,
    RetractReason,
    ReviewerKind,
    ReviewerVerdict,
    ReviewRejectReason,
    ReviewVerdict,
)
from .rules import RuleReviewer
from .source import (
    ReviewSource,
    claim_hash,
    line_is_located,
    load_review_source,
    snippet_hash,
)


def subjects_from_drafts(drafts: Sequence[TriageDraft]) -> list[CrossCheckSubject]:
    return [
        CrossCheckSubject(
            finding_id=draft.finding_id,
            rule_id=draft.rule_id,
            source=draft.source,
            file=draft.file,
            line_start=draft.line_start,
            line_end=draft.line_end,
            claim=draft.claim,
            trigger_path=list(draft.trigger_path),
        )
        for draft in drafts
    ]


def subjects_from_candidates(
    rule_report: RuleScanReport, triage_report: TriageReport
) -> list[CrossCheckSubject]:
    """Build one subject per rule candidate, including untriaged candidates.

    Grading requires exactly one cross-check result per candidate; candidates
    whose triage was rejected still get reviewed as plain rule findings.
    """
    drafts = {draft.finding_id: draft for draft in triage_report.drafts}
    subjects: list[CrossCheckSubject] = []
    for candidate in rule_report.candidates:
        draft = drafts.get(candidate.finding_id)
        if draft is None:
            subjects.append(
                CrossCheckSubject(
                    finding_id=candidate.finding_id,
                    rule_id=candidate.rule_id,
                    source=candidate.source,
                    file=candidate.relative_file,
                    line_start=candidate.line_start,
                    line_end=candidate.line_end,
                    claim=candidate.message,
                )
            )
        else:
            subjects.append(
                CrossCheckSubject(
                    finding_id=draft.finding_id,
                    rule_id=draft.rule_id,
                    source=draft.source,
                    file=draft.file,
                    line_start=draft.line_start,
                    line_end=draft.line_end,
                    claim=draft.claim,
                    trigger_path=list(draft.trigger_path),
                )
            )
    return subjects


def subjects_from_independent_claims(
    report: IndependentReport,
) -> list[CrossCheckSubject]:
    """One semantic subject per accepted claim; rules are not involved."""
    return [
        CrossCheckSubject(
            finding_id=claim.finding_id,
            rule_id=None,
            source=FindingSource.LLM_INDEPENDENT,
            file=claim.file,
            line_start=claim.line_start,
            line_end=claim.line_end,
            claim=claim.claim,
            trigger_path=list(claim.trigger_path),
        )
        for claim in report.claims
    ]


def _unverifiable(reason: RetractReason) -> ReviewerVerdict:
    return ReviewerVerdict(verdict=ReviewVerdict.UNVERIFIABLE, retract_reason=reason)


def _downgrade_to_unverifiable(
    verdict: ReviewerVerdict, reason: RetractReason
) -> ReviewerVerdict:
    return verdict.model_copy(
        update={"verdict": ReviewVerdict.UNVERIFIABLE, "retract_reason": reason}
    )


def _validated_verdict(
    verdict: ReviewerVerdict,
    source_for: Callable[[str], ReviewSource | None],
) -> ReviewerVerdict:
    if verdict.verdict == ReviewVerdict.UNVERIFIABLE:
        if verdict.retract_reason is None:
            return verdict.model_copy(
                update={"retract_reason": RetractReason.UNVERIFIABLE_CLAIM}
            )
        return verdict

    required_role = (
        EvidenceRole.SUPPORTS
        if verdict.verdict == ReviewVerdict.PASS
        else EvidenceRole.REFUTES
    )
    required = [
        line for line in verdict.evidence_lines if line.role == required_role
    ]
    if not required:
        reason = (
            RetractReason.NO_LINE_LOCATION
            if verdict.evidence_lines
            else RetractReason.UNVERIFIABLE_CLAIM
        )
        return _downgrade_to_unverifiable(verdict, reason)

    for line in required:
        source = source_for(line.file)
        if source is None or not line_is_located(
            source, line.line_start, line.line_end
        ):
            return _downgrade_to_unverifiable(
                verdict, RetractReason.NO_LINE_LOCATION
            )
        expected = snippet_hash(source.snippet(line.line_start, line.line_end))
        if expected != line.snippet_hash:
            return _downgrade_to_unverifiable(
                verdict, RetractReason.NO_LINE_LOCATION
            )

    if verdict.verdict == ReviewVerdict.REFUTE and verdict.retract_reason is None:
        return verdict.model_copy(
            update={"retract_reason": RetractReason.REFUTED_BY_CONTEXT}
        )
    return verdict


def cross_check(
    profile: ProjectProfile,
    rule_report: RuleScanReport,
    subjects: Sequence[CrossCheckSubject],
    reviewer: ClaimReviewer,
) -> CrossCheckReport:
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    root = Path(profile.root).expanduser().resolve()
    rule_reviewer = RuleReviewer()

    sources: dict[str, tuple[ReviewSource | None, str | None]] = {}

    def source_for(relative_file: str) -> ReviewSource | None:
        if relative_file not in sources:
            sources[relative_file] = load_review_source(root, relative_file)
        return sources[relative_file][0]

    results: list[CrossCheckResult] = []
    verdict_counts: Counter[ReviewVerdict] = Counter()
    rejection_counts: Counter[ReviewRejectReason] = Counter()
    warnings: set[str] = set()
    rule_checks = 0
    model_reviews = 0

    for subject in subjects:
        subject_started = time.perf_counter()
        if subject.file not in sources:
            sources[subject.file] = load_review_source(root, subject.file)
        source, read_error = sources[subject.file]
        context = build_review_context(subject, source, rule_report)

        rule_backed = subject.rule_id is not None
        if source is None:
            verdict = _unverifiable(RetractReason.UNVERIFIABLE_CLAIM)
            reviewer_kind = (
                ReviewerKind.RULE if rule_backed else reviewer.reviewer_kind
            )
            reviewer_id = (
                rule_reviewer.reviewer_id if rule_backed else reviewer.reviewer_id
            )
            if rule_backed:
                rule_checks += 1
            warnings.add(f"source unavailable for {subject.file}: {read_error}")
        elif not line_is_located(source, subject.line_start, subject.line_end):
            verdict = _unverifiable(RetractReason.NO_LINE_LOCATION)
            reviewer_kind = (
                ReviewerKind.RULE if rule_backed else reviewer.reviewer_kind
            )
            reviewer_id = (
                rule_reviewer.reviewer_id if rule_backed else reviewer.reviewer_id
            )
            if rule_backed:
                rule_checks += 1
        elif rule_backed:
            verdict = rule_reviewer.review(subject, source)
            reviewer_kind = rule_reviewer.reviewer_kind
            reviewer_id = rule_reviewer.reviewer_id
            rule_checks += 1
        else:
            request = build_review_request(subject, source, context)
            verdict = reviewer.review(request)
            reviewer_kind = reviewer.reviewer_kind
            reviewer_id = reviewer.reviewer_id
            model_reviews += 1

        verdict = _validated_verdict(verdict, source_for)
        verdict_counts[verdict.verdict] += 1
        if verdict.rejection_reason is not None:
            rejection_counts[verdict.rejection_reason] += 1

        results.append(
            CrossCheckResult(
                finding_id=subject.finding_id,
                claim_hash=claim_hash(subject.claim),
                verdict=verdict.verdict,
                evidence_lines=verdict.evidence_lines,
                retract_reason=verdict.retract_reason,
                checked_rule_ids=list(verdict.checked_rule_ids),
                reviewer_kind=reviewer_kind,
                reviewer_id=reviewer_id,
                source_context_hash=context.source_context_hash,
                elapsed_ms=int((time.perf_counter() - subject_started) * 1000),
                rejection_reason=verdict.rejection_reason,
            )
        )

    if not subjects:
        warnings.add("no cross-check subjects")
    for warning in getattr(reviewer, "warnings", []):
        warnings.add(warning)

    results.sort(key=lambda result: result.finding_id)

    trace = CrossCheckTrace(
        started_at=started_at,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        subjects_considered=len(subjects),
        verdict_counts=dict(
            sorted(verdict_counts.items(), key=lambda item: item[0].value)
        ),
        rule_checks=rule_checks,
        model_reviews=model_reviews,
        reviewer_ids=sorted({result.reviewer_id for result in results}),
        review_rejections=dict(
            sorted(
                rejection_counts.items(), key=lambda item: item[0].value
            )
        ),
        warnings=sorted(warnings),
    )
    return CrossCheckReport(results=results, trace=trace)
