from __future__ import annotations

import json
from pathlib import Path

import pytest

from audit.grade import grade_and_emit
from audit.independent import independent_request_id, scan_independent
from audit.repro import run_repro
from audit.rule_scan import scan_rules
from audit.triage import llm_triage
from helpers import make_root_profile
from llm.fake import DeterministicFakeLLM
from llm.models import FinishReason, LLMCallStats, LLMPurpose, LLMRequest, LLMResponse, ModelKind
from llm.recording import RecordingLLM
from llm.replay import ReplayLLM
from review.cross_check import (
    cross_check,
    subjects_from_candidates,
    subjects_from_independent_claims,
)
from review.fake import DeterministicReviewer
from review.llm_reviewer import (
    REVIEW_TEMPLATE_VERSION,
    REVIEWER_ID,
    LLMReviewer,
    build_review_prompt,
)
from review.models import (
    EvidenceKind,
    EvidenceRole,
    ReviewContext,
    ReviewRejectReason,
    ReviewerKind,
    ReviewRequest,
    ReviewVerdict,
)
from sandbox.runner import SandboxConfig

SOURCE = '''\
def handler(value):
    if value is None:
        return 0
    return int(value)


def collect(items=[]):
    return items
'''

FINDING_ID = "indep::test"


def _project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(SOURCE, encoding="utf-8")
    return tmp_path


def _request(
    *,
    file: str = "app.py",
    line_start: int = 2,
    line_end: int = 2,
    claim: str = "None is guarded but raw input is not validated",
) -> ReviewRequest:
    return ReviewRequest(
        request_id=f"cross_check::{FINDING_ID}",
        finding_id=FINDING_ID,
        rule_id=None,
        file=file,
        line_start=line_start,
        line_end=line_end,
        claim=claim,
        trigger_path=["value", "int conversion"],
        subject_snippet="if value is None:",
        subject_snippet_hash="0" * 16,
        context=ReviewContext(file=file, file_line_count=len(SOURCE.splitlines())),
    )


def _evidence(
    *,
    quote: str = "if value is None:",
    kind: str = "guard",
    role: str = "supports",
    explanation: str = "the None guard is present",
) -> dict:
    return {
        "quote": quote,
        "kind": kind,
        "role": role,
        "explanation": explanation,
    }


def _response(verdict: str = "pass", evidence: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "evidence": evidence if evidence is not None else [_evidence()],
        },
        sort_keys=True,
    )


def _reviewer(tmp_path: Path, text: str) -> tuple[LLMReviewer, DeterministicFakeLLM]:
    project = _project(tmp_path)
    fake = DeterministicFakeLLM({f"cross_check::{FINDING_ID}": text})
    return LLMReviewer(make_root_profile(project), fake), fake


def test_pass_anchors_evidence_and_labels_the_model(tmp_path):
    reviewer, fake = _reviewer(tmp_path, _response())

    verdict = reviewer.review(_request())

    assert verdict.verdict is ReviewVerdict.PASS
    assert verdict.rejection_reason is None
    line = verdict.evidence_lines[0]
    assert line.file == "app.py"
    assert line.line_start == 2
    assert line.line_end == 2
    assert line.evidence_kind is EvidenceKind.GUARD
    assert line.role is EvidenceRole.SUPPORTS
    assert line.snippet == "    if value is None:"
    assert len(line.snippet_hash) == 16
    assert reviewer.reviewer_kind is ReviewerKind.MODEL
    assert reviewer.reviewer_id == REVIEWER_ID
    assert fake.calls[0].purpose is LLMPurpose.CLAIM_REVIEW
    assert fake.calls[0].prompt_template_version == REVIEW_TEMPLATE_VERSION
    assert "<REVIEW_JSON>" in fake.calls[0].prompt
    assert "triager" not in fake.calls[0].prompt


def test_refute_is_anchored_with_refutes_evidence(tmp_path):
    reviewer, _ = _reviewer(
        tmp_path,
        _response(
            "refute",
            [
                _evidence(
                    quote="return int(value)",
                    kind="data_flow",
                    role="refutes",
                    explanation="the value reaches int() directly",
                )
            ],
        ),
    )

    verdict = reviewer.review(_request())

    assert verdict.verdict is ReviewVerdict.REFUTE
    assert verdict.evidence_lines[0].role is EvidenceRole.REFUTES
    assert verdict.evidence_lines[0].line_start == 4


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", ReviewRejectReason.EMPTY_RESPONSE),
        ("{not json", ReviewRejectReason.INVALID_JSON),
        ('{"verdict": "pass"}', ReviewRejectReason.INVALID_SHAPE),
        ('{"verdict": "maybe", "evidence": []}', ReviewRejectReason.UNKNOWN_VERDICT),
        (
            _response(evidence=[_evidence(role="sideways")]),
            ReviewRejectReason.UNKNOWN_ROLE,
        ),
        (
            _response(evidence=[_evidence(kind="vibes")]),
            ReviewRejectReason.UNKNOWN_KIND,
        ),
        (
            _response(evidence=[_evidence(explanation="   ")]),
            ReviewRejectReason.EMPTY_EXPLANATION,
        ),
        (
            _response(evidence=[_evidence(quote="   ")]),
            ReviewRejectReason.EMPTY_QUOTE,
        ),
        (
            _response(evidence=[_evidence(quote="x" * 401)]),
            ReviewRejectReason.QUOTE_TOO_LONG,
        ),
        (
            _response(evidence=[_evidence(quote="not in this file")]),
            ReviewRejectReason.UNLOCATABLE_QUOTE,
        ),
        (
            _response(evidence=[_evidence(quote="return")]),
            ReviewRejectReason.AMBIGUOUS_QUOTE,
        ),
    ],
)
def test_rejections_are_enumerated_and_unverifiable(tmp_path, text, reason):
    reviewer, _ = _reviewer(tmp_path, text)

    verdict = reviewer.review(_request())

    assert verdict.verdict is ReviewVerdict.UNVERIFIABLE
    assert verdict.rejection_reason is reason
    assert verdict.evidence_lines == []
    assert verdict.retract_reason is not None
    assert verdict.retract_reason.value == "UNVERIFIABLE_CLAIM"


def test_model_supplied_line_fields_are_ignored(tmp_path):
    item = _evidence()
    item["file"] = "elsewhere.py"
    item["line_start"] = 999
    item["line_end"] = 999
    item["snippet_hash"] = "f" * 16
    reviewer, _ = _reviewer(tmp_path, _response(evidence=[item]))

    verdict = reviewer.review(_request())

    line = verdict.evidence_lines[0]
    assert line.file == "app.py"
    assert line.line_start == 2
    assert line.snippet_hash != "f" * 16


def test_default_fake_returns_unverifiable(tmp_path):
    project = _project(tmp_path)
    reviewer = LLMReviewer(make_root_profile(project), DeterministicFakeLLM())

    verdict = reviewer.review(_request())

    assert verdict.verdict is ReviewVerdict.UNVERIFIABLE
    assert verdict.rejection_reason is None
    assert verdict.evidence_lines == []


def test_prompt_carries_the_review_context_without_triage_reasoning():
    prompt = build_review_prompt(_request(claim="guard missing"))

    assert "<REVIEW_JSON>" in prompt
    assert "indep::test" in prompt
    assert "guard missing" in prompt
    assert "app.py" in prompt


def test_reviewer_is_independent_of_the_triage_and_deterministic_fakes():
    reviewer = LLMReviewer(
        make_root_profile(Path(".")), DeterministicFakeLLM()
    )
    triage = DeterministicFakeLLM()
    deterministic = DeterministicReviewer()

    assert type(reviewer) is not type(triage)
    assert type(reviewer) is not type(deterministic)
    assert LLMReviewer.__module__ != DeterministicFakeLLM.__module__
    assert LLMReviewer.__module__ != DeterministicReviewer.__module__
    assert not hasattr(reviewer, "complete")
    assert not hasattr(triage, "review")


def _independent_pipeline(tmp_path: Path, review_text: str):
    project = tmp_path
    (project / "app.py").write_text(
        """\
def handler(value):
    if value is None:
        return 0
    return int(value)
""",
        encoding="utf-8",
    )
    profile = make_root_profile(project)
    scan = scan_rules(profile)
    claim = {
        "claims": [
            {
                "claim_kind": "MISSING_INPUT_VALIDATION",
                "quote": "return int(value)",
                "claim": "raw input is converted without validation",
                "trigger_path": ["value", "int conversion"],
            }
        ]
    }
    independent = scan_independent(
        profile,
        scan,
        DeterministicFakeLLM(
            {
                independent_request_id("app.py", "handler"): json.dumps(
                    claim, sort_keys=True
                )
            }
        ),
    )
    finding_id = independent.claims[0].finding_id
    fake = DeterministicFakeLLM(
        {f"cross_check::{finding_id}": review_text}
    )
    results = cross_check(
        profile,
        scan,
        subjects_from_independent_claims(independent),
        LLMReviewer(profile, fake),
    )
    triage = llm_triage(profile, scan, DeterministicFakeLLM())
    repro = run_repro(
        profile,
        scan,
        results,
        sandbox_config=SandboxConfig(double_run=False),
        artifact_root=tmp_path,
    )
    report = grade_and_emit(
        scan, triage, results, repro, independent_report=independent
    )
    return independent, results, report


def test_cross_check_confirms_a_semantic_claim(tmp_path):
    item = _evidence(
        quote="return int(value)",
        kind="data_flow",
        role="supports",
        explanation="the raw value reaches int()",
    )
    independent, results, report = _independent_pipeline(
        tmp_path, _response(evidence=[item])
    )

    assert results.results[0].verdict is ReviewVerdict.PASS
    assert results.results[0].reviewer_kind is ReviewerKind.MODEL
    assert results.results[0].rejection_reason is None
    assert report.trace.independent_findings_emitted == 1
    finding = report.findings[0]
    assert finding.finding_id == independent.claims[0].finding_id
    assert finding.source.value == "LLM_INDEPENDENT"
    assert finding.evidence_level.value == "STATIC_CONFIRMED"


def test_cross_check_aggregates_rejection_reasons(tmp_path):
    item = _evidence(quote="not in this file")
    _, results, report = _independent_pipeline(
        tmp_path, _response(evidence=[item])
    )

    result = results.results[0]
    assert result.verdict is ReviewVerdict.UNVERIFIABLE
    assert result.rejection_reason is ReviewRejectReason.UNLOCATABLE_QUOTE
    assert results.trace.review_rejections == {
        ReviewRejectReason.UNLOCATABLE_QUOTE: 1
    }
    assert report.retracted[0].reason.value == "UNVERIFIABLE_CLAIM"


def test_rule_backed_claims_never_reach_the_llm_reviewer(tmp_path):
    project = _project(tmp_path)
    profile = make_root_profile(project)
    scan = scan_rules(profile)
    fake = DeterministicFakeLLM()
    triage = llm_triage(profile, scan, DeterministicFakeLLM())

    results = cross_check(
        profile,
        scan,
        subjects_from_candidates(scan, triage),
        LLMReviewer(profile, fake),
    )

    assert results.trace.rule_checks == 1
    assert results.trace.model_reviews == 0
    assert fake.calls == []


class _RealInner:
    model_kind = ModelKind.REAL
    model_id = "recorded-reviewer"

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls: list[LLMRequest] = []
        self.stats = LLMCallStats()

    def complete(self, request: LLMRequest) -> LLMResponse:
        text = self._texts.pop(0) if self._texts else ""
        self.calls.append(request)
        self.stats.calls += 1
        return LLMResponse(
            request_id=request.request_id,
            text=text,
            model_id=self.model_id,
            model_kind=self.model_kind,
            finish_reason=FinishReason.STOP,
        )


def test_review_record_and_replay_are_offline(tmp_path):
    project = _project(tmp_path)
    profile = make_root_profile(project)
    transcript = tmp_path / "review_transcript.json"
    request = _request()

    recorded = LLMReviewer(
        profile, RecordingLLM(_RealInner([_response()]), transcript)
    ).review(request)
    verdict = LLMReviewer(profile, ReplayLLM(transcript)).review(request)

    assert recorded.verdict is ReviewVerdict.PASS
    assert verdict.verdict is ReviewVerdict.PASS
    assert verdict.evidence_lines[0].line_start == 2

    from llm.errors import ReplayError, ReplayErrorReason

    missing = LLMRequest(
        request_id="cross_check::unrecorded",
        purpose=LLMPurpose.CLAIM_REVIEW,
        prompt_template_version=REVIEW_TEMPLATE_VERSION,
        system="",
        prompt="prompt",
    )
    with pytest.raises(ReplayError) as excinfo:
        ReplayLLM(transcript).complete(missing)
    assert excinfo.value.replay_reason is ReplayErrorReason.MISSING_RESPONSE
