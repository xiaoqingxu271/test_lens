from __future__ import annotations

from pathlib import Path

from audit.models import FindingSource, RuleId
from audit.rule_scan import scan_rules
from audit.triage import llm_triage
from helpers import line_of, make_root_profile
from llm.fake import DeterministicFakeLLM
from review.cross_check import cross_check, subjects_from_drafts
from review.fake import DeterministicReviewer
from review.models import (
    CrossCheckSubject,
    EvidenceKind,
    EvidenceLine,
    EvidenceRole,
    RetractReason,
    ReviewerKind,
    ReviewerVerdict,
    ReviewVerdict,
)
from review.source import claim_hash, load_review_source, make_evidence

REVIEW_ROOT = Path(__file__).parent / "fixtures" / "audit_project" / "review"
DEFECTS = REVIEW_ROOT / "defects.py"


def _setup():
    profile = make_root_profile(REVIEW_ROOT)
    scan = scan_rules(profile)
    drafts = llm_triage(profile, scan, DeterministicFakeLLM()).drafts
    return profile, scan, drafts


def _candidate_at(scan, line: int):
    return next(
        candidate for candidate in scan.candidates if candidate.line_start == line
    )


def _result_at(report, scan, line: int):
    finding_id = _candidate_at(scan, line).finding_id
    return next(
        result for result in report.results if result.finding_id == finding_id
    )


def _semantic_subject(line_start: int, line_end: int | None = None) -> CrossCheckSubject:
    return CrossCheckSubject(
        finding_id="semantic-none",
        rule_id=None,
        source=FindingSource.RULE_WITH_LLM_TRIAGE,
        file="defects.py",
        line_start=line_start,
        line_end=line_end if line_end is not None else line_start,
        claim="x is dereferenced after being assigned None",
        trigger_path=["x = None", "return x.value"],
    )


def _stable(report):
    data = report.model_dump(mode="json")
    data["trace"].pop("started_at", None)
    data["trace"].pop("elapsed_ms", None)
    for result in data["results"]:
        result.pop("elapsed_ms", None)
    return data


def test_rule_reviewer_confirms_matches_and_refutes_r005_false_positives():
    profile, scan, drafts = _setup()
    reviewer = DeterministicReviewer()

    report = cross_check(profile, scan, subjects_from_drafts(drafts), reviewer)

    assert len(scan.candidates) == 5
    assert report.trace.subjects_considered == 5
    assert report.trace.rule_checks == 5
    assert report.trace.model_reviews == 0
    assert report.trace.verdict_counts == {
        ReviewVerdict.PASS: 3,
        ReviewVerdict.REFUTE: 2,
    }
    assert report.trace.reviewer_ids == ["rule-reviewer-v1"]
    assert reviewer.requests == []

    mutable = _result_at(
        report, scan, line_of(DEFECTS, "def mutable_default")
    )
    assert mutable.verdict == ReviewVerdict.PASS
    assert mutable.retract_reason is None
    assert mutable.checked_rule_ids == [RuleId.R001_MUTABLE_DEFAULT.value]
    assert mutable.reviewer_kind == ReviewerKind.RULE
    assert mutable.claim_hash and len(mutable.claim_hash) == 16
    assert mutable.source_context_hash
    evidence = mutable.evidence_lines[0]
    assert evidence.evidence_kind == EvidenceKind.RULE
    assert evidence.role == EvidenceRole.SUPPORTS
    assert evidence.line_start == line_of(DEFECTS, "def mutable_default")

    try_finally = _result_at(
        report,
        scan,
        line_of(DEFECTS, 'handle = open("data.txt")', after="def try_finally_close"),
    )
    assert try_finally.verdict == ReviewVerdict.REFUTE
    assert try_finally.retract_reason == RetractReason.REFUTED_BY_CONTEXT
    evidence = try_finally.evidence_lines[0]
    assert evidence.evidence_kind == EvidenceKind.DATA_FLOW
    assert evidence.role == EvidenceRole.REFUTES
    assert evidence.line_start == line_of(DEFECTS, "handle.close()")
    assert "try/finally" in evidence.explanation

    exit_stack = _result_at(
        report,
        scan,
        line_of(DEFECTS, 'open("data.txt")', after="def exit_stack_managed"),
    )
    assert exit_stack.verdict == ReviewVerdict.REFUTE
    assert exit_stack.retract_reason == RetractReason.REFUTED_BY_CONTEXT
    evidence = exit_stack.evidence_lines[0]
    assert evidence.evidence_kind == EvidenceKind.GUARD
    assert evidence.role == EvidenceRole.REFUTES
    assert evidence.line_start == line_of(DEFECTS, "stack.enter_context")
    assert "ExitStack" in evidence.explanation


def test_rule_reviewer_refutes_a_stale_anchor():
    profile, scan, _ = _setup()
    subject = CrossCheckSubject(
        finding_id="stale-r005",
        rule_id=RuleId.R005_RESOURCE_NO_WITH,
        file="defects.py",
        line_start=line_of(DEFECTS, "handle.read()"),
        line_end=line_of(DEFECTS, "handle.read()"),
        claim="resource opened without with",
    )

    report = cross_check(profile, scan, [subject], DeterministicReviewer())

    result = report.results[0]
    assert result.verdict == ReviewVerdict.REFUTE
    assert result.retract_reason == RetractReason.REFUTED_BY_CONTEXT
    assert result.checked_rule_ids == [RuleId.R005_RESOURCE_NO_WITH.value]
    evidence = result.evidence_lines[0]
    assert evidence.evidence_kind == EvidenceKind.RULE
    assert evidence.role == EvidenceRole.REFUTES
    assert evidence.line_start == subject.line_start
    assert "no longer matches" in evidence.explanation


def test_semantic_claim_requires_an_explicit_verdict():
    profile, scan, _ = _setup()
    span = line_of(DEFECTS, "return x.value", after="def none_deref")
    subject = _semantic_subject(span)
    reviewer = DeterministicReviewer()

    report = cross_check(profile, scan, [subject], reviewer)

    result = report.results[0]
    assert result.verdict == ReviewVerdict.UNVERIFIABLE
    assert result.retract_reason == RetractReason.UNVERIFIABLE_CLAIM
    assert result.reviewer_kind == ReviewerKind.FAKE
    assert result.reviewer_id == reviewer.reviewer_id
    assert result.checked_rule_ids == []
    assert result.claim_hash == claim_hash(subject.claim)
    assert result.evidence_lines == []
    assert report.trace.rule_checks == 0
    assert report.trace.model_reviews == 1
    assert report.trace.verdict_counts == {ReviewVerdict.UNVERIFIABLE: 1}

    assert len(reviewer.requests) == 1
    request = reviewer.requests[0]
    assert request.request_id == "cross_check::semantic-none"
    assert request.subject_snippet.strip() == "return x.value"
    assert request.context.enclosing_function is not None
    assert request.context.enclosing_function.name == "none_deref"


def test_registered_semantic_pass_keeps_anchored_evidence():
    profile, scan, _ = _setup()
    span = line_of(DEFECTS, "return x.value", after="def none_deref")
    subject = _semantic_subject(span)
    source, error = load_review_source(REVIEW_ROOT, "defects.py")
    assert source is not None and error is None
    evidence = make_evidence(
        source,
        span,
        span,
        kind=EvidenceKind.DATA_FLOW,
        role=EvidenceRole.SUPPORTS,
        explanation="the dereference follows an explicit None assignment",
    )
    verdict = ReviewerVerdict(
        verdict=ReviewVerdict.PASS, evidence_lines=[evidence]
    )

    report = cross_check(
        profile,
        scan,
        [subject],
        DeterministicReviewer({"semantic-none": verdict}),
    )

    result = report.results[0]
    assert result.verdict == ReviewVerdict.PASS
    assert result.evidence_lines[0].line_start == span
    assert result.evidence_lines[0].role == EvidenceRole.SUPPORTS
    assert result.evidence_lines[0].evidence_kind == EvidenceKind.DATA_FLOW


def test_independent_reviewer_refutes_with_anchored_evidence():
    profile, scan, _ = _setup()
    span = line_of(DEFECTS, "return x.value", after="def none_deref")
    subject = _semantic_subject(span)
    source, error = load_review_source(REVIEW_ROOT, "defects.py")
    assert source is not None and error is None

    guard_line = line_of(DEFECTS, "if x is not None")
    evidence = make_evidence(
        source,
        guard_line,
        guard_line,
        kind=EvidenceKind.GUARD,
        role=EvidenceRole.REFUTES,
        explanation="an existing guard prevents the dereference",
    )
    verdict = ReviewerVerdict(
        verdict=ReviewVerdict.REFUTE,
        evidence_lines=[evidence],
        retract_reason=RetractReason.REFUTED_BY_CONTEXT,
    )

    report = cross_check(
        profile,
        scan,
        [subject],
        DeterministicReviewer({"semantic-none": verdict}),
    )

    result = report.results[0]
    assert result.verdict == ReviewVerdict.REFUTE
    assert result.retract_reason == RetractReason.REFUTED_BY_CONTEXT
    assert result.evidence_lines[0].line_start == guard_line
    assert result.evidence_lines[0].role == EvidenceRole.REFUTES


def test_missing_or_unlocatable_evidence_is_downgraded():
    profile, scan, _ = _setup()
    span = line_of(DEFECTS, "return x.value", after="def none_deref")
    subject = _semantic_subject(span)

    no_evidence = DeterministicReviewer(
        {"semantic-none": ReviewerVerdict(verdict=ReviewVerdict.PASS)}
    )
    report = cross_check(profile, scan, [subject], no_evidence)
    result = report.results[0]
    assert result.verdict == ReviewVerdict.UNVERIFIABLE
    assert result.retract_reason == RetractReason.UNVERIFIABLE_CLAIM

    bad_hash = ReviewerVerdict(
        verdict=ReviewVerdict.PASS,
        evidence_lines=[
            EvidenceLine(
                file="defects.py",
                line_start=span,
                line_end=span,
                evidence_kind=EvidenceKind.DATA_FLOW,
                role=EvidenceRole.SUPPORTS,
                explanation="hash does not match the source",
                snippet_hash="0" * 16,
            )
        ],
    )
    report = cross_check(
        profile,
        scan,
        [subject],
        DeterministicReviewer({"semantic-none": bad_hash}),
    )
    result = report.results[0]
    assert result.verdict == ReviewVerdict.UNVERIFIABLE
    assert result.retract_reason == RetractReason.NO_LINE_LOCATION

    out_of_range = ReviewerVerdict(
        verdict=ReviewVerdict.PASS,
        evidence_lines=[
            EvidenceLine(
                file="defects.py",
                line_start=999,
                line_end=999,
                evidence_kind=EvidenceKind.DATA_FLOW,
                role=EvidenceRole.SUPPORTS,
                explanation="line does not exist",
                snippet_hash="0" * 16,
            )
        ],
    )
    report = cross_check(
        profile,
        scan,
        [subject],
        DeterministicReviewer({"semantic-none": out_of_range}),
    )
    result = report.results[0]
    assert result.verdict == ReviewVerdict.UNVERIFIABLE
    assert result.retract_reason == RetractReason.NO_LINE_LOCATION


def test_unreadable_source_is_unverifiable_without_reviewer_call():
    profile, scan, _ = _setup()
    rule_subject = CrossCheckSubject(
        finding_id="missing-rule",
        rule_id=RuleId.R001_MUTABLE_DEFAULT,
        file="missing.py",
        line_start=1,
        line_end=1,
        claim="mutable default",
    )
    semantic_subject = CrossCheckSubject(
        finding_id="missing-semantic",
        rule_id=None,
        file="missing.py",
        line_start=1,
        line_end=1,
        claim="semantic claim",
    )
    reviewer = DeterministicReviewer()

    report = cross_check(
        profile, scan, [rule_subject, semantic_subject], reviewer
    )

    assert [result.verdict for result in report.results] == [
        ReviewVerdict.UNVERIFIABLE,
        ReviewVerdict.UNVERIFIABLE,
    ]
    assert all(
        result.retract_reason == RetractReason.UNVERIFIABLE_CLAIM
        for result in report.results
    )
    assert reviewer.requests == []
    assert report.trace.rule_checks == 1
    assert report.trace.model_reviews == 0
    assert any(
        "source unavailable for missing.py" in warning
        for warning in report.trace.warnings
    )


def test_cross_check_is_deterministic():
    profile, scan, drafts = _setup()

    first = cross_check(
        profile, scan, subjects_from_drafts(drafts), DeterministicReviewer()
    )
    second = cross_check(
        profile, scan, subjects_from_drafts(drafts), DeterministicReviewer()
    )

    assert _stable(first) == _stable(second)


def test_reviewer_fake_is_independent_from_the_triage_fake():
    reviewer = DeterministicReviewer()
    triage = DeterministicFakeLLM()

    assert type(reviewer) is not type(triage)
    assert DeterministicReviewer.__module__ != DeterministicFakeLLM.__module__
    assert not isinstance(reviewer, DeterministicFakeLLM)
    assert not hasattr(reviewer, "complete")
    assert not hasattr(triage, "review")
    assert reviewer.requests == []
    assert triage.calls == []
