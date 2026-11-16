from __future__ import annotations

import json
from pathlib import Path

from audit.models import (
    FindingSource,
    RuleCandidate,
    RuleId,
    RuleScanReport,
    RuleScanTrace,
    TriageRejectReason,
)
from audit.rule_scan import scan_rules
from audit.triage import TRIAGE_TEMPLATE_VERSION, llm_triage, triage_request_id
from helpers import make_root_profile
from llm.fake import DeterministicFakeLLM
from llm.models import ModelKind

AUDIT_ROOT = Path(__file__).parent / "fixtures" / "audit_project"


def _stable(report):
    data = report.model_dump(mode="json")
    data["trace"].pop("started_at", None)
    data["trace"].pop("elapsed_ms", None)
    return data


def _synthetic_report(tmp_path: Path, count: int = 8):
    source = "\n".join(f"value_{index} = {index}" for index in range(1, count + 1)) + "\n"
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")

    candidates = [
        RuleCandidate(
            finding_id=f"finding-{index}",
            rule_id=RuleId.R001_MUTABLE_DEFAULT,
            file=str(path),
            relative_file="sample.py",
            line_start=index,
            line_end=index,
            column=0,
            title="Mutable default argument",
            message=f"Mutable default on line {index}",
            snippet=f"value_{index} = {index}",
            snippet_hash=f"hash-{index}",
        )
        for index in range(1, count + 1)
    ]
    trace = RuleScanTrace(
        started_at="2026-01-01T00:00:00+00:00",
        elapsed_ms=0,
        files_scanned=1,
        candidates_found=count,
        candidates_by_rule={RuleId.R001_MUTABLE_DEFAULT: count},
        rules_run=[RuleId.R001_MUTABLE_DEFAULT],
    )
    return RuleScanReport(candidates=candidates, trace=trace), candidates


def _payload(candidate: RuleCandidate, **overrides) -> str:
    data = {
        "finding_id": candidate.finding_id,
        "rule_id": candidate.rule_id.value,
        "line_start": candidate.line_start,
        "line_end": candidate.line_end,
        "trigger_path": ["step one", "step two"],
        "claim": "the trigger path reaches the defect",
    }
    data.update(overrides)
    return json.dumps(data)


def test_llm_triage_completes_every_rule_candidate():
    profile = make_root_profile(AUDIT_ROOT / "r004")
    scan = scan_rules(profile, rules=(RuleId.R004_BARE_EXCEPT_SWALLOW,))
    fake = DeterministicFakeLLM()

    report = llm_triage(profile, scan, fake)

    assert len(scan.candidates) == 4
    assert len(report.drafts) == 4
    assert report.rejected == []
    assert [draft.finding_id for draft in report.drafts] == [
        candidate.finding_id for candidate in scan.candidates
    ]

    for candidate, draft in zip(scan.candidates, report.drafts):
        assert draft.rule_id == candidate.rule_id
        assert draft.file == candidate.relative_file
        assert (draft.line_start, draft.line_end) == (
            candidate.line_start,
            candidate.line_end,
        )
        assert draft.source == FindingSource.RULE_WITH_LLM_TRIAGE
        assert draft.trigger_path
        assert draft.claim
        assert draft.prompt_template_version == TRIAGE_TEMPLATE_VERSION
        assert draft.model_kind == ModelKind.FAKE
        assert draft.prompt_digest and draft.response_digest

    assert [call.request_id for call in fake.calls] == [
        triage_request_id(candidate.finding_id) for candidate in scan.candidates
    ]
    assert "<CANDIDATE_JSON>" in fake.calls[0].prompt

    trace = report.trace
    assert trace.candidates_considered == 4
    assert trace.drafts_accepted == 4
    assert trace.drafts_rejected == 0
    assert trace.rejected_by_reason == {}
    assert trace.warnings == []


def test_llm_triage_is_deterministic():
    profile = make_root_profile(AUDIT_ROOT / "r004")
    scan = scan_rules(profile, rules=(RuleId.R004_BARE_EXCEPT_SWALLOW,))

    first = llm_triage(profile, scan, DeterministicFakeLLM())
    second = llm_triage(profile, scan, DeterministicFakeLLM())

    assert _stable(first) == _stable(second)


def test_llm_triage_rejects_anchor_drift_and_malformed_responses(tmp_path):
    scan, candidates = _synthetic_report(tmp_path, 8)
    responses = {
        triage_request_id(candidates[0].finding_id): "not json",
        triage_request_id(candidates[1].finding_id): "",
        triage_request_id(candidates[2].finding_id): _payload(
            candidates[2], finding_id="finding-unknown"
        ),
        triage_request_id(candidates[3].finding_id): _payload(
            candidates[3], rule_id=RuleId.R002_NONE_DEREF_NO_GUARD.value
        ),
        triage_request_id(candidates[4].finding_id): _payload(
            candidates[4],
            line_start=candidates[4].line_start + 1,
            line_end=candidates[4].line_end + 1,
        ),
        triage_request_id(candidates[5].finding_id): _payload(
            candidates[5], trigger_path=[]
        ),
        triage_request_id(candidates[6].finding_id): _payload(
            candidates[6], trigger_path="step"
        ),
        triage_request_id(candidates[7].finding_id): _payload(
            candidates[7], claim="   "
        ),
    }

    report = llm_triage(
        make_root_profile(tmp_path),
        scan,
        DeterministicFakeLLM(responses),
    )

    assert report.drafts == []
    assert report.trace.drafts_rejected == 8
    assert report.trace.rejected_by_reason == {
        TriageRejectReason.EMPTY_RESPONSE: 1,
        TriageRejectReason.INVALID_JSON: 1,
        TriageRejectReason.INVALID_SHAPE: 1,
        TriageRejectReason.UNKNOWN_FINDING: 1,
        TriageRejectReason.RULE_MISMATCH: 1,
        TriageRejectReason.LINE_ANCHOR_MISMATCH: 1,
        TriageRejectReason.NO_TRIGGER_PATH: 1,
        TriageRejectReason.EMPTY_CLAIM: 1,
    }

    by_reason = {item.reason: item for item in report.rejected}
    assert by_reason[TriageRejectReason.LINE_ANCHOR_MISMATCH].finding_id == "finding-5"
    assert "expected lines 5-5" in by_reason[
        TriageRejectReason.LINE_ANCHOR_MISMATCH
    ].detail
    assert by_reason[TriageRejectReason.UNKNOWN_FINDING].file == "sample.py"


def test_llm_triage_warns_when_there_are_no_candidates(tmp_path):
    scan = RuleScanReport(
        candidates=[],
        trace=RuleScanTrace(
            started_at="2026-01-01T00:00:00+00:00",
            elapsed_ms=0,
            files_scanned=0,
        ),
    )

    report = llm_triage(make_root_profile(tmp_path), scan, DeterministicFakeLLM())

    assert report.drafts == []
    assert report.rejected == []
    assert report.trace.candidates_considered == 0
    assert report.trace.warnings == ["no rule candidates to triage"]
