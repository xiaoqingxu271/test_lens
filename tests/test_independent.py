from __future__ import annotations

import json
from pathlib import Path

import pytest

from audit.grade import grade_and_emit
from audit.independent import (
    claim_title,
    independent_request_id,
    scan_independent,
)
from audit.models import (
    ClaimKind,
    EvidenceLevel,
    FindingSource,
    IndependentRejectReason,
    RetractReason,
)
from audit.repro import run_repro
from audit.rule_scan import scan_rules
from audit.triage import llm_triage
from helpers import line_of, make_root_profile
from llm.fake import DeterministicFakeLLM
from review.cross_check import (
    cross_check,
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
from sandbox.runner import SandboxConfig

HANDLER_SOURCE = '''\
def handler(request):
    limit = int(request.args["limit"])
    return limit


def empty_guard(value):
    if value is None:
        return 0
    return value + 1
'''

MUTABLE_SOURCE = '''\
def mutable(items=[]):
    return items
'''


def _project(tmp_path: Path, source: str = HANDLER_SOURCE) -> Path:
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    return tmp_path


def _response(claims: list[dict]) -> str:
    return json.dumps({"claims": claims}, sort_keys=True)


def _claim(
    *,
    kind: str = ClaimKind.MISSING_INPUT_VALIDATION.value,
    quote: str = 'limit = int(request.args["limit"])',
    claim: str = "The limit is converted without validating input.",
    trigger_path: list[str] | None = None,
) -> dict:
    return {
        "claim_kind": kind,
        "quote": quote,
        "claim": claim,
        "trigger_path": (
            trigger_path if trigger_path is not None else ["request.args", "int conversion"]
        ),
    }


def _fake(**responses: str) -> DeterministicFakeLLM:
    return DeterministicFakeLLM(
        {
            independent_request_id("app.py", name): payload
            for name, payload in responses.items()
        }
    )


def test_scan_anchors_a_claim_from_a_verbatim_quote(tmp_path):
    project = _project(tmp_path)
    profile = make_root_profile(project)
    scan = scan_rules(profile)
    fake = _fake(handler=_response([_claim()]))

    report = scan_independent(profile, scan, fake)

    assert len(report.claims) == 1
    claim = report.claims[0]
    expected_line = line_of(project / "app.py", 'limit = int(request.args["limit"])')
    assert claim.line_start == expected_line
    assert claim.line_end == expected_line
    assert claim.file == "app.py"
    assert claim.symbol_id == "app.py::handler"
    assert claim.claim_kind is ClaimKind.MISSING_INPUT_VALIDATION
    assert claim.finding_id.startswith("indep::")
    assert len(claim.snippet_hash) == 16
    assert claim_title(claim) == "MISSING_INPUT_VALIDATION: handler"
    assert report.rejected == []
    assert report.trace.functions_considered == 2
    assert report.trace.requests_sent == 2
    assert report.trace.claims_received == 1
    assert report.trace.claims_accepted == 1
    assert report.trace.claims_rejected == 0
    assert report.trace.llm_calls == 2


def test_default_fake_proposes_no_claims(tmp_path):
    project = _project(tmp_path)
    profile = make_root_profile(project)

    report = scan_independent(profile, scan_rules(profile), DeterministicFakeLLM())

    assert report.claims == []
    assert report.rejected == []
    assert report.trace.claims_received == 0
    assert report.trace.requests_sent == 2


def test_scope_is_module_functions_in_file_order(tmp_path):
    (tmp_path / "app.py").write_text(
        '''\
def zeta(value):
    def nested():
        return 1
    return value


class Helper:
    def method(self):
        return 2


async def alpha(value):
    return value
''',
        encoding="utf-8",
    )
    profile = make_root_profile(tmp_path)
    fake = DeterministicFakeLLM()

    report = scan_independent(profile, scan_rules(profile), fake)

    assert [call.request_id for call in fake.calls] == [
        "llm_independent::app.py::zeta",
        "llm_independent::app.py::alpha",
    ]
    assert report.trace.functions_considered == 2
    assert report.trace.requests_sent == 2


def test_claim_kinds_share_an_anchor_with_distinct_ids(tmp_path):
    project = _project(tmp_path)
    profile = make_root_profile(project)
    fake = _fake(
        handler=_response(
            [
                _claim(kind=ClaimKind.MISSING_INPUT_VALIDATION.value),
                _claim(kind=ClaimKind.UNSAFE_SINK.value),
            ]
        )
    )

    report = scan_independent(profile, scan_rules(profile), fake)

    assert len(report.claims) == 2
    kinds = {claim.claim_kind for claim in report.claims}
    assert kinds == {
        ClaimKind.MISSING_INPUT_VALIDATION,
        ClaimKind.UNSAFE_SINK,
    }
    assert len({claim.finding_id for claim in report.claims}) == 2
    titles = {claim_title(claim) for claim in report.claims}
    assert titles == {
        "MISSING_INPUT_VALIDATION: handler",
        "UNSAFE_SINK: handler",
    }


def test_invalid_claims_are_rejected_by_reason(tmp_path):
    project = _project(tmp_path)
    profile = make_root_profile(project)
    scan = scan_rules(profile)
    too_long = "x" * 401
    fake = _fake(
        handler=_response(
            [
                _claim(kind="NOT_A_KIND"),
                _claim(claim="   "),
                _claim(quote="   "),
                _claim(quote=too_long),
                _claim(quote="not present in the function"),
                _claim(trigger_path=[]),
                {"claim_kind": "MISSING_INPUT_VALIDATION", "quote": "return limit"},
            ]
        ),
        empty_guard=_response([_claim(quote="value")]),
    )

    report = scan_independent(profile, scan, fake)

    reasons = {item.reason for item in report.rejected}
    assert IndependentRejectReason.UNSUPPORTED_CLAIM_KIND in reasons
    assert IndependentRejectReason.EMPTY_CLAIM in reasons
    assert IndependentRejectReason.EMPTY_QUOTE in reasons
    assert IndependentRejectReason.QUOTE_TOO_LONG in reasons
    assert IndependentRejectReason.UNLOCATABLE_QUOTE in reasons
    assert IndependentRejectReason.INVALID_SHAPE in reasons
    assert IndependentRejectReason.AMBIGUOUS_QUOTE in reasons
    assert report.claims == []
    assert report.trace.claims_rejected == len(report.rejected)
    assert sum(report.trace.rejected_by_reason.values()) == len(report.rejected)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("", IndependentRejectReason.EMPTY_RESPONSE),
        ("{not json", IndependentRejectReason.INVALID_JSON),
        ('{"claims": "nope"}', IndependentRejectReason.INVALID_SHAPE),
        ('{"other": []}', IndependentRejectReason.INVALID_SHAPE),
    ],
)
def test_malformed_responses_are_rejected(tmp_path, payload, reason):
    project = _project(tmp_path)
    profile = make_root_profile(project)
    fake = _fake(handler=payload)

    report = scan_independent(profile, scan_rules(profile), fake)

    assert any(item.reason is reason for item in report.rejected)


def test_unreadable_files_are_reported_without_a_request(tmp_path):
    _project(tmp_path)
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    profile = make_root_profile(tmp_path)
    fake = DeterministicFakeLLM()

    report = scan_independent(profile, scan_rules(profile), fake)

    assert report.claims == []
    assert [item.file for item in report.rejected] == ["broken.py"]
    assert report.rejected[0].reason is IndependentRejectReason.SOURCE_UNREADABLE
    assert report.trace.functions_considered == 2
    assert all("broken.py" not in call.request_id for call in fake.calls)


def test_claims_overlapping_rule_candidates_are_rejected(tmp_path):
    project = _project(tmp_path, MUTABLE_SOURCE)
    profile = make_root_profile(project)
    scan = scan_rules(profile)
    assert len(scan.candidates) == 1
    fake = _fake(mutable=_response([_claim(quote="def mutable(items=[]):")]))

    report = scan_independent(profile, scan, fake)

    assert report.claims == []
    assert [item.reason for item in report.rejected] == [
        IndependentRejectReason.DUPLICATE_OF_RULE
    ]


def test_duplicate_anchors_keep_the_first_claim(tmp_path):
    project = _project(tmp_path)
    profile = make_root_profile(project)
    fake = _fake(
        handler=_response(
            [
                _claim(claim="first wording"),
                _claim(claim="second wording"),
            ]
        )
    )

    report = scan_independent(profile, scan_rules(profile), fake)

    assert len(report.claims) == 1
    assert report.claims[0].claim == "first wording"
    assert [item.reason for item in report.rejected] == [
        IndependentRejectReason.DUPLICATE_CLAIM
    ]


def _pipeline(tmp_path: Path, reviewer_verdicts=None, source=HANDLER_SOURCE):
    project = _project(tmp_path, source)
    profile = make_root_profile(project)
    scan = scan_rules(profile)
    independent = scan_independent(
        profile, scan, _fake(handler=_response([_claim()]))
    )
    results = cross_check(
        profile,
        scan,
        subjects_from_independent_claims(independent),
        DeterministicReviewer(reviewer_verdicts),
    )
    repro = run_repro(
        profile,
        scan,
        results,
        sandbox_config=SandboxConfig(double_run=False),
        artifact_root=tmp_path,
    )
    report = grade_and_emit(
        scan,
        llm_triage(profile, scan, DeterministicFakeLLM()),
        results,
        repro,
        independent_report=independent,
    )
    return independent, results, report


def test_registered_pass_becomes_static_confirmed(tmp_path):
    independent = scan_independent(
        make_root_profile(_project(tmp_path)),
        scan_rules(make_root_profile(tmp_path)),
        _fake(handler=_response([_claim()])),
    )
    claim = independent.claims[0]
    source, error = load_review_source(tmp_path, "app.py")
    assert source is not None and error is None
    evidence = make_evidence(
        source,
        claim.line_start,
        claim.line_end,
        kind=EvidenceKind.DATA_FLOW,
        role=EvidenceRole.SUPPORTS,
        explanation="input reaches int() without validation",
    )
    _, _, report = _pipeline(
        tmp_path,
        {
            claim.finding_id: ReviewerVerdict(
                verdict=ReviewVerdict.PASS, evidence_lines=[evidence]
            )
        },
    )

    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.source == FindingSource.LLM_INDEPENDENT
    assert finding.rule_id is None
    assert finding.llm_finding_id is None
    assert finding.claim_kind is ClaimKind.MISSING_INPUT_VALIDATION
    assert finding.evidence_level == EvidenceLevel.STATIC_CONFIRMED
    assert finding.repro_artifact is None
    assert report.folded_suspicious == []
    assert report.trace.independent_candidates_considered == 1
    assert report.trace.independent_findings_emitted == 1


def test_unregistered_semantic_claim_is_retracted(tmp_path):
    _, _, report = _pipeline(tmp_path)

    assert report.findings == []
    assert report.folded_suspicious == []
    assert len(report.retracted) == 1
    assert report.retracted[0].reason == RetractReason.UNVERIFIABLE_CLAIM
    assert report.retracted[0].finding.source == FindingSource.LLM_INDEPENDENT
    assert report.trace.independent_findings_emitted == 0
    assert report.trace.independent_retracted == 1


def test_missing_independent_result_is_a_pipeline_error(tmp_path):
    project = _project(tmp_path)
    profile = make_root_profile(project)
    scan = scan_rules(profile)
    independent = scan_independent(
        profile, scan, _fake(handler=_response([_claim()]))
    )
    triage = llm_triage(profile, scan, DeterministicFakeLLM())
    review = cross_check(profile, scan, [], DeterministicReviewer())

    with pytest.raises(ValueError):
        grade_and_emit(
            scan,
            triage,
            review,
            run_repro(
                profile,
                scan,
                review,
                sandbox_config=SandboxConfig(double_run=False),
                artifact_root=tmp_path,
            ),
            independent_report=independent,
        )
