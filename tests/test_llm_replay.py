from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import make_root_profile, sandbox_health

from audit.independent import (
    INDEPENDENT_TEMPLATE_VERSION,
    scan_independent,
)
from audit.rule_scan import scan_rules
from audit.triage import llm_triage
from generation.generate import generate_tests
from llm.errors import (
    ReplayError,
    ReplayErrorReason,
    TranscriptError,
    TranscriptErrorReason,
)
from llm.models import (
    FinishReason,
    LLMCallStats,
    LLMPurpose,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ModelKind,
)
from llm.recording import RecordingLLM
from llm.replay import (
    TRANSCRIPT_VERSION,
    LLMTranscript,
    ReplayLLM,
    load_transcript,
)
from targets.selection import select_targets

MODEL_ID = "recorded-model-1"
FIXTURES = Path(__file__).parent / "fixtures"
REPLAY_FIXTURE = FIXTURES / "llm" / "replay_transcript.json"


class _RealInner:
    """Deterministic inner client that labels itself as a real model."""

    model_kind = ModelKind.REAL

    def __init__(
        self,
        texts: list[str] | None = None,
        *,
        usage: LLMUsage | None = None,
        model_id: str = MODEL_ID,
    ) -> None:
        self.model_id = model_id
        self._texts = list(texts or [])
        self._usage = usage or LLMUsage(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )
        self.calls: list[LLMRequest] = []
        self.stats = LLMCallStats()

    def complete(self, request: LLMRequest) -> LLMResponse:
        text = self._texts.pop(0) if self._texts else '{"tests": []}'
        self.calls.append(request)
        self.stats.calls += 1
        self.stats.prompt_tokens += self._usage.prompt_tokens or 0
        self.stats.completion_tokens += self._usage.completion_tokens or 0
        return LLMResponse(
            request_id=request.request_id,
            text=text,
            model_id=self.model_id,
            model_kind=self.model_kind,
            finish_reason=FinishReason.STOP,
            usage=self._usage,
            attempts=1,
        )


def _request(
    request_id: str = "test_generation::app.py::read_item",
    *,
    purpose: LLMPurpose = LLMPurpose.TEST_GENERATION,
    template: str = "generation-v1",
    system: str = "system prompt",
    prompt: str = "user prompt",
) -> LLMRequest:
    return LLMRequest(
        request_id=request_id,
        purpose=purpose,
        prompt_template_version=template,
        system=system,
        prompt=prompt,
    )


def _record(
    path: Path,
    requests: list[LLMRequest],
    texts: list[str],
    *,
    usage: LLMUsage | None = None,
) -> None:
    inner = _RealInner(texts, usage=usage)
    recorder = RecordingLLM(inner, path)
    for request in requests:
        recorder.complete(request)


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _rewrite(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def test_record_then_replay_roundtrip(tmp_path):
    path = tmp_path / "transcript.json"
    requests = [
        _request("test_generation::app.py::read_item", prompt="prompt one"),
        _request(
            "rule_triage::finding-1",
            purpose=LLMPurpose.RULE_TRIAGE,
            template="triage-v1",
            prompt="prompt two",
        ),
    ]
    _record(path, requests, ['{"tests": []}', '{"claim": "x"}'])

    replay = ReplayLLM(path)
    first = replay.complete(requests[0])
    second = replay.complete(requests[1])

    assert replay.model_kind is ModelKind.REAL
    assert replay.model_id == MODEL_ID
    assert first.text == '{"tests": []}'
    assert first.replayed is True
    assert first.model_kind is ModelKind.REAL
    assert first.attempts == 1
    assert first.usage.total_tokens == 15
    assert second.text == '{"claim": "x"}'
    assert replay.stats.calls == 2
    assert replay.stats.prompt_tokens == 20
    assert replay.stats.completion_tokens == 10
    assert replay.stats.replayed is True


def test_repeated_replay_is_identical_and_order_independent(tmp_path):
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    requests = [
        _request("test_generation::b", prompt="b"),
        _request("test_generation::a", prompt="a"),
    ]
    _record(first_path, requests, ["text for b", "text for a"])
    _record(second_path, list(reversed(requests)), ["text for a", "text for b"])

    assert first_path.read_bytes() == second_path.read_bytes()
    assert [item["request_id"] for item in _payload(first_path)["recordings"]] == [
        "test_generation::a",
        "test_generation::b",
    ]

    def dump() -> list[dict]:
        replay = ReplayLLM(first_path)
        return [
            replay.complete(request).model_dump(mode="json")
            for request in requests
        ]

    assert dump() == dump()


def test_replay_returns_the_recorded_model_identity(tmp_path):
    path = tmp_path / "transcript.json"
    _record(path, [_request()], ['{"tests": []}'])
    payload = _payload(path)
    payload["model_id"] = "other-model"
    for recording in payload["recordings"]:
        recording["model_id"] = "other-model"
    _rewrite(path, payload)

    response = ReplayLLM(path).complete(_request())

    assert response.model_id == "other-model"


def test_tampered_text_is_rejected_on_load(tmp_path):
    path = tmp_path / "transcript.json"
    _record(path, [_request()], ['{"tests": []}'])
    payload = _payload(path)
    payload["recordings"][0]["text"] = '{"tests": ["tampered"]}'
    _rewrite(path, payload)

    with pytest.raises(ReplayError) as excinfo:
        load_transcript(path)

    assert excinfo.value.replay_reason is ReplayErrorReason.RESPONSE_DIGEST_MISMATCH


def test_prompt_digest_mismatch_is_rejected_on_complete(tmp_path):
    path = tmp_path / "transcript.json"
    request = _request()
    _record(path, [request], ['{"tests": []}'])
    payload = _payload(path)
    payload["recordings"][0]["prompt_digest"] = "0" * 16
    _rewrite(path, payload)

    with pytest.raises(ReplayError) as excinfo:
        ReplayLLM(path).complete(request)

    assert excinfo.value.replay_reason is ReplayErrorReason.PROMPT_DIGEST_MISMATCH


def test_template_version_mismatch_is_rejected(tmp_path):
    path = tmp_path / "transcript.json"
    request = _request()
    _record(path, [request], ['{"tests": []}'])
    payload = _payload(path)
    payload["recordings"][0]["prompt_template_version"] = "generation-v2"
    _rewrite(path, payload)

    with pytest.raises(ReplayError) as excinfo:
        ReplayLLM(path).complete(request)

    assert (
        excinfo.value.replay_reason is ReplayErrorReason.TEMPLATE_VERSION_MISMATCH
    )


def test_purpose_mismatch_is_rejected(tmp_path):
    path = tmp_path / "transcript.json"
    request = _request()
    _record(path, [request], ['{"tests": []}'])
    payload = _payload(path)
    payload["recordings"][0]["purpose"] = "rule_triage"
    _rewrite(path, payload)

    with pytest.raises(ReplayError) as excinfo:
        ReplayLLM(path).complete(request)

    assert excinfo.value.replay_reason is ReplayErrorReason.PURPOSE_MISMATCH


def test_missing_response_never_falls_back(tmp_path):
    path = tmp_path / "transcript.json"
    _record(path, [_request("test_generation::recorded")], ['{"tests": []}'])

    with pytest.raises(ReplayError) as excinfo:
        ReplayLLM(path).complete(_request("test_generation::unrecorded"))

    assert excinfo.value.replay_reason is ReplayErrorReason.MISSING_RESPONSE


def test_duplicate_request_ids_are_invalid(tmp_path):
    path = tmp_path / "transcript.json"
    _record(path, [_request()], ['{"tests": []}'])
    payload = _payload(path)
    payload["recordings"].append(dict(payload["recordings"][0]))
    _rewrite(path, payload)

    with pytest.raises(TranscriptError) as excinfo:
        load_transcript(path)

    assert excinfo.value.transcript_reason is TranscriptErrorReason.DUPLICATE_REQUEST


def test_unsupported_transcript_version_is_invalid(tmp_path):
    path = tmp_path / "transcript.json"
    _record(path, [_request()], ['{"tests": []}'])
    payload = _payload(path)
    payload["transcript_version"] = TRANSCRIPT_VERSION + 1
    _rewrite(path, payload)

    with pytest.raises(TranscriptError) as excinfo:
        load_transcript(path)

    assert (
        excinfo.value.transcript_reason
        is TranscriptErrorReason.UNSUPPORTED_VERSION
    )


def test_recording_model_id_must_match_the_transcript(tmp_path):
    path = tmp_path / "transcript.json"
    _record(path, [_request()], ['{"tests": []}'])
    payload = _payload(path)
    payload["recordings"][0]["model_id"] = "other-model"
    _rewrite(path, payload)

    with pytest.raises(TranscriptError) as excinfo:
        load_transcript(path)

    assert excinfo.value.transcript_reason is TranscriptErrorReason.INVALID


def test_expected_model_mismatch_is_rejected(tmp_path):
    path = tmp_path / "transcript.json"
    _record(path, [_request()], ['{"tests": []}'])

    with pytest.raises(ReplayError) as excinfo:
        load_transcript(path, expected_model_id="another-model")

    assert excinfo.value.replay_reason is ReplayErrorReason.MODEL_MISMATCH


def test_fake_recordings_are_not_replayable(tmp_path):
    path = tmp_path / "transcript.json"
    _record(path, [_request()], ['{"tests": []}'])
    payload = _payload(path)
    payload["model_kind"] = ModelKind.FAKE.value
    _rewrite(path, payload)

    with pytest.raises(TranscriptError) as excinfo:
        load_transcript(path)

    assert excinfo.value.transcript_reason is TranscriptErrorReason.INVALID


def test_invalid_transcript_json_is_enumerated(tmp_path):
    path = tmp_path / "transcript.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(TranscriptError) as excinfo:
        load_transcript(path)

    assert excinfo.value.transcript_reason is TranscriptErrorReason.INVALID


def test_unreadable_transcript_is_enumerated(tmp_path):
    with pytest.raises(TranscriptError) as excinfo:
        load_transcript(tmp_path / "missing.json")

    assert excinfo.value.transcript_reason is TranscriptErrorReason.UNREADABLE


def test_recorder_refuses_to_overwrite(tmp_path):
    path = tmp_path / "transcript.json"
    _record(path, [_request()], ['{"tests": []}'])

    with pytest.raises(Exception) as excinfo:
        RecordingLLM(_RealInner(), path)

    assert "already exists" in str(excinfo.value)

    replacement = RecordingLLM(_RealInner(["replacement"]), path, overwrite=True)
    replacement.complete(_request())

    assert load_transcript(path).recordings[0].text == "replacement"


def test_recorder_stores_no_prompts_or_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTLENS_LLM_API_KEY", "super-secret-key")
    path = tmp_path / "transcript.json"
    system = "SYSTEM-MARKER-42"
    prompt = "PROMPT-MARKER-99"
    _record(
        path,
        [_request(system=system, prompt=prompt)],
        ['{"tests": []}'],
    )

    text = path.read_text(encoding="utf-8")

    assert "super-secret-key" not in text
    assert system not in text
    assert prompt not in text
    payload = _payload(path)
    recording = payload["recordings"][0]
    assert recording["prompt_digest"]
    assert "system" not in recording
    assert "prompt" not in recording
    assert "headers" not in recording


def test_recorder_write_failures_are_errors(tmp_path):
    path = tmp_path / "directory"
    path.mkdir()

    with pytest.raises(Exception) as excinfo:
        RecordingLLM(_RealInner(), path, overwrite=True)

    assert "could not write transcript" in str(excinfo.value)


def test_recorder_rejects_fake_and_mismatched_responses(tmp_path):
    class _FakeKind(_RealInner):
        model_kind = ModelKind.FAKE

    with pytest.raises(Exception) as fake_error:
        RecordingLLM(_FakeKind(), tmp_path / "fake.json").complete(_request())
    assert "only real model responses" in str(fake_error.value)

    class _WrongId(_RealInner):
        def complete(self, request: LLMRequest) -> LLMResponse:
            response = super().complete(request)
            return response.model_copy(update={"request_id": "other"})

    with pytest.raises(Exception) as id_error:
        RecordingLLM(_WrongId(), tmp_path / "wrong.json").complete(_request())
    assert "does not match" in str(id_error.value)


def test_vendored_transcript_replays_generation_end_to_end():
    profile = make_root_profile(FIXTURES / "routes_project")
    selection = select_targets(profile)
    replay = ReplayLLM(REPLAY_FIXTURE)

    report = generate_tests(
        profile, selection, replay, sandbox_health=sandbox_health()
    )

    assert len(selection.targets) == 9
    assert len(report.candidates) == 9
    assert report.trace.replayed is True
    assert report.trace.llm_calls == 9
    assert report.trace.prompt_tokens > 0
    assert report.trace.llm_model_kind is ModelKind.REAL
    assert report.trace.llm_model_id == "fixture-recorded-model"


def test_vendored_transcript_replays_triage_end_to_end():
    profile = make_root_profile(FIXTURES / "audit_project" / "r001")
    scan = scan_rules(profile)
    replay = ReplayLLM(REPLAY_FIXTURE)

    report = llm_triage(profile, scan, replay)

    assert len(scan.candidates) == 7
    assert len(report.drafts) == 7
    assert report.trace.replayed is True
    assert report.trace.llm_calls == 7
    assert report.trace.llm_model_kind is ModelKind.REAL


def test_independent_scan_records_and_replays_offline(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text(
        """\
def handler(value):
    if value > 0:
        return 1
    return 0
""",
        encoding="utf-8",
    )
    transcript_path = tmp_path / "transcript.json"
    claim = json.dumps(
        {
            "claims": [
                {
                    "claim_kind": "MISSING_INPUT_VALIDATION",
                    "quote": "if value > 0:",
                    "claim": "value is compared without validation",
                    "trigger_path": ["handler parameter"],
                }
            ]
        },
        sort_keys=True,
    )
    profile = make_root_profile(project)
    scan = scan_rules(profile)

    recorded = scan_independent(
        profile, scan, RecordingLLM(_RealInner([claim]), transcript_path)
    )
    replayed = scan_independent(profile, scan, ReplayLLM(transcript_path))

    assert len(recorded.claims) == 1
    assert len(replayed.claims) == 1
    assert replayed.claims[0].finding_id == recorded.claims[0].finding_id
    assert replayed.claims[0].model_kind is ModelKind.REAL
    assert replayed.trace.replayed is True
    assert replayed.trace.llm_calls == 1
    assert replayed.trace.llm_model_id == MODEL_ID

    missing = LLMRequest(
        request_id="llm_independent::app.py::unrecorded",
        purpose=LLMPurpose.LLM_INDEPENDENT,
        prompt_template_version=INDEPENDENT_TEMPLATE_VERSION,
        system="system",
        prompt="prompt",
    )
    with pytest.raises(ReplayError) as excinfo:
        ReplayLLM(transcript_path).complete(missing)
    assert excinfo.value.replay_reason is ReplayErrorReason.MISSING_RESPONSE


def test_replay_fixture_does_not_cover_other_projects():
    profile = make_root_profile(FIXTURES / "sample_project")
    selection = select_targets(profile)
    assert selection.targets == []

    request = _request("test_generation::unknown.py::handler")

    with pytest.raises(ReplayError) as excinfo:
        ReplayLLM(REPLAY_FIXTURE).complete(request)

    assert excinfo.value.replay_reason is ReplayErrorReason.MISSING_RESPONSE


def test_transcript_model_roundtrips_through_pydantic():
    transcript = LLMTranscript(
        transcript_version=TRANSCRIPT_VERSION,
        created_by="testlens",
        model_id=MODEL_ID,
    )

    restored = LLMTranscript.model_validate(
        transcript.model_dump(mode="json")
    )

    assert restored.model_kind is ModelKind.REAL
    assert restored.recordings == []
