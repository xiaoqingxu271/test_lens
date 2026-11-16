from __future__ import annotations

from pathlib import Path

import pytest

from audit.grade import grade_and_emit
from audit.models import (
    EvidenceLevel,
    FindingSource,
    ReproReport,
    ReproTrace,
    RetractReason,
    RuleCandidate,
    RuleId,
    RuleScanReport,
    RuleScanTrace,
    TriageReport,
    TriageTrace,
)
from audit.repro import run_repro
from audit.rule_scan import scan_rules
from audit.triage import llm_triage
from helpers import line_of, make_root_profile
from llm.fake import DeterministicFakeLLM
from llm.models import ModelKind
from review.cross_check import (
    cross_check,
    subjects_from_candidates,
    subjects_from_drafts,
)
from review.fake import DeterministicReviewer
from review.models import (
    CrossCheckReport,
    CrossCheckResult,
    CrossCheckTrace,
    EvidenceKind,
    EvidenceLine,
    EvidenceRole,
    ReviewerKind,
    ReviewVerdict,
)
from sandbox.runner import SandboxConfig

FIXTURES = Path(__file__).parent / "fixtures" / "audit_project"
REVIEW_ROOT = FIXTURES / "review"
REPRO_ROOT = FIXTURES / "repro"
REVIEW_DEFECTS = REVIEW_ROOT / "defects.py"
REPRO_DEFECTS = REPRO_ROOT / "defects.py"

TIMESTAMP = "2026-01-01T00:00:00+00:00"


def _pipeline(root: Path, artifact_root: Path):
    profile = make_root_profile(root)
    scan = scan_rules(profile)
    triage = llm_triage(profile, scan, DeterministicFakeLLM())
    review = cross_check(
        profile, scan, subjects_from_drafts(triage.drafts), DeterministicReviewer()
    )
    repro = run_repro(
        profile,
        scan,
        review,
        sandbox_config=SandboxConfig(double_run=False),
        artifact_root=artifact_root,
    )
    report = grade_and_emit(scan, triage, review, repro)
    return scan, triage, review, repro, report


def _finding_at(report, line: int):
    return next(
        finding for finding in report.findings if finding.line_start == line
    )


def test_grading_emits_reproduced_static_and_retracted(tmp_path):
    scan, triage, review, repro, report = _pipeline(REVIEW_ROOT, tmp_path)

    assert report.trace.candidates_considered == 5
    assert report.trace.findings_emitted == 3
    assert report.trace.folded_suspicious == 0
    assert report.trace.retracted == 2
    assert report.trace.evidence_level_counts == {
        EvidenceLevel.REPRODUCED: 1,
        EvidenceLevel.STATIC_CONFIRMED: 2,
    }
    assert report.trace.retract_reason_counts == {
        RetractReason.REFUTED_BY_CONTEXT: 2
    }
    assert report.trace.repro_attempted == 1
    assert report.trace.repro_reproduced == 1

    mutable = _finding_at(report, line_of(REVIEW_DEFECTS, "def mutable_default"))
    assert mutable.rule_id == RuleId.R001_MUTABLE_DEFAULT
    assert mutable.evidence_level == EvidenceLevel.REPRODUCED
    assert mutable.repro_artifact is not None
    assert mutable.repro_artifact.reproduced is True
    assert mutable.source == FindingSource.RULE_WITH_LLM_TRIAGE
    assert mutable.llm_finding_id == mutable.finding_id
    assert mutable.claim
    assert mutable.trigger_path

    none_deref = _finding_at(report, line_of(REVIEW_DEFECTS, "return x.value", after="def none_deref"))
    assert none_deref.evidence_level == EvidenceLevel.STATIC_CONFIRMED
    assert none_deref.repro_artifact is None

    bare_except = _finding_at(report, line_of(REVIEW_DEFECTS, "except:", after="def bare_except"))
    assert bare_except.evidence_level == EvidenceLevel.STATIC_CONFIRMED

    assert {item.finding.rule_id for item in report.retracted} == {
        RuleId.R005_RESOURCE_NO_WITH
    }
    assert all(
        item.reason == RetractReason.REFUTED_BY_CONTEXT
        for item in report.retracted
    )
    assert all(
        item.finding.evidence_level == EvidenceLevel.SUSPICIOUS
        for item in report.retracted
    )


def test_failed_repro_stays_static_confirmed(tmp_path):
    scan, triage, review, repro, report = _pipeline(REPRO_ROOT, tmp_path)

    assert report.trace.candidates_considered == 8
    assert report.trace.findings_emitted == 8
    assert report.trace.retracted == 0
    assert report.trace.folded_suspicious == 0
    assert report.trace.evidence_level_counts == {
        EvidenceLevel.REPRODUCED: 5,
        EvidenceLevel.STATIC_CONFIRMED: 3,
    }

    copied = _finding_at(report, line_of(REPRO_DEFECTS, "def copied_default"))
    assert copied.evidence_level == EvidenceLevel.STATIC_CONFIRMED
    assert copied.repro_artifact is not None
    assert copied.repro_artifact.reproduced is False

    required = _finding_at(report, line_of(REPRO_DEFECTS, "def required_arg"))
    assert required.evidence_level == EvidenceLevel.STATIC_CONFIRMED
    assert required.repro_artifact is None

    nested = _finding_at(report, line_of(REPRO_DEFECTS, "def nested"))
    assert nested.evidence_level == EvidenceLevel.STATIC_CONFIRMED


def _synthetic_inputs(
    *,
    verdict: ReviewVerdict,
    evidence: list[EvidenceLine] | None = None,
    checked_rule_ids: list[str] | None = None,
    retract_reason: RetractReason | None = None,
):
    candidate = RuleCandidate(
        finding_id="f1",
        rule_id=RuleId.R001_MUTABLE_DEFAULT,
        file="/project/x.py",
        relative_file="x.py",
        line_start=1,
        line_end=1,
        column=0,
        title="Mutable default argument",
        message="mutable default",
        snippet="def f(items=[]):",
        snippet_hash="hash",
    )
    rule_report = RuleScanReport(
        candidates=[candidate],
        trace=RuleScanTrace(
            started_at=TIMESTAMP,
            elapsed_ms=0,
            files_scanned=1,
            candidates_found=1,
        ),
    )
    triage_report = TriageReport(
        drafts=[],
        rejected=[],
        trace=TriageTrace(
            started_at=TIMESTAMP,
            elapsed_ms=0,
            llm_model_kind=ModelKind.FAKE,
            llm_model_id="fake",
            prompt_template_version="triage-v1",
        ),
    )
    cross_report = CrossCheckReport(
        results=[
            CrossCheckResult(
                finding_id="f1",
                claim_hash="claim",
                verdict=verdict,
                evidence_lines=list(evidence or []),
                retract_reason=retract_reason,
                checked_rule_ids=list(checked_rule_ids or []),
                reviewer_kind=ReviewerKind.RULE,
                reviewer_id="rule-reviewer-v1",
                source_context_hash="context",
                elapsed_ms=0,
            )
        ]
        if verdict is not None
        else [],
        trace=CrossCheckTrace(started_at=TIMESTAMP, elapsed_ms=0),
    )
    repro_report = ReproReport(
        artifacts=[],
        skipped=[],
        trace=ReproTrace(started_at=TIMESTAMP, elapsed_ms=0),
    )
    return rule_report, triage_report, cross_report, repro_report


def test_pass_without_evidence_is_folded_as_suspicious():
    rule_report, triage_report, cross_report, repro_report = _synthetic_inputs(
        verdict=ReviewVerdict.PASS
    )

    report = grade_and_emit(rule_report, triage_report, cross_report, repro_report)

    assert report.findings == []
    assert len(report.folded_suspicious) == 1
    assert report.folded_suspicious[0].evidence_level == EvidenceLevel.SUSPICIOUS
    assert report.trace.folded_suspicious == 1
    assert report.trace.evidence_level_counts == {EvidenceLevel.SUSPICIOUS: 1}


def test_unverifiable_verdict_retracts_with_its_reason():
    rule_report, triage_report, cross_report, repro_report = _synthetic_inputs(
        verdict=ReviewVerdict.UNVERIFIABLE,
        retract_reason=RetractReason.NO_LINE_LOCATION,
    )

    report = grade_and_emit(rule_report, triage_report, cross_report, repro_report)

    assert report.findings == []
    assert len(report.retracted) == 1
    assert report.retracted[0].reason == RetractReason.NO_LINE_LOCATION
    assert report.trace.retract_reason_counts == {
        RetractReason.NO_LINE_LOCATION: 1
    }


def test_rule_confirmation_supports_static_confirmed():
    evidence = EvidenceLine(
        file="x.py",
        line_start=1,
        line_end=1,
        evidence_kind=EvidenceKind.RULE,
        role=EvidenceRole.SUPPORTS,
        explanation="rule still matches",
        snippet_hash="hash",
        snippet="def f(items=[]):",
    )
    rule_report, triage_report, cross_report, repro_report = _synthetic_inputs(
        verdict=ReviewVerdict.PASS,
        evidence=[evidence],
        checked_rule_ids=[RuleId.R001_MUTABLE_DEFAULT.value],
    )

    report = grade_and_emit(rule_report, triage_report, cross_report, repro_report)

    assert len(report.findings) == 1
    assert report.findings[0].evidence_level == EvidenceLevel.STATIC_CONFIRMED
    assert report.findings[0].source == FindingSource.RULE
    assert report.findings[0].llm_finding_id is None


def test_missing_cross_check_result_is_a_pipeline_error():
    rule_report, triage_report, cross_report, repro_report = _synthetic_inputs(
        verdict=None
    )

    with pytest.raises(ValueError):
        grade_and_emit(rule_report, triage_report, cross_report, repro_report)


def test_untriaged_candidates_are_reviewed_as_rule_findings(tmp_path):
    profile = make_root_profile(REVIEW_ROOT)
    scan = scan_rules(profile)
    empty_triage = TriageReport(
        drafts=[],
        rejected=[],
        trace=TriageTrace(
            started_at=TIMESTAMP,
            elapsed_ms=0,
            llm_model_kind=ModelKind.FAKE,
            llm_model_id="fake",
            prompt_template_version="triage-v1",
        ),
    )

    subjects = subjects_from_candidates(scan, empty_triage)
    review = cross_check(profile, scan, subjects, DeterministicReviewer())
    repro = run_repro(
        profile,
        scan,
        review,
        sandbox_config=SandboxConfig(double_run=False),
        artifact_root=tmp_path,
    )
    report = grade_and_emit(scan, empty_triage, review, repro)

    assert len(subjects) == len(scan.candidates)
    assert len(review.results) == len(scan.candidates)
    assert all(
        finding.source == FindingSource.RULE for finding in report.findings
    )
    assert all(
        finding.llm_finding_id is None for finding in report.findings
    )
