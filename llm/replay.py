"""Deterministic transcript replay for recorded model responses."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .errors import (
    ReplayError,
    ReplayErrorReason,
    TranscriptError,
    TranscriptErrorReason,
)
from .models import (
    FinishReason,
    LLMCallStats,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ModelKind,
    digest_text,
)

TRANSCRIPT_VERSION = 1


class TranscriptRecording(BaseModel):
    request_id: str
    purpose: str
    prompt_template_version: str
    prompt_digest: str
    response_digest: str
    text: str
    model_id: str
    model_kind: ModelKind
    finish_reason: FinishReason = FinishReason.STOP
    attempts: int = 1
    usage: LLMUsage = Field(default_factory=LLMUsage)


class LLMTranscript(BaseModel):
    transcript_version: int = TRANSCRIPT_VERSION
    created_by: str = ""
    model_id: str
    model_kind: ModelKind = ModelKind.REAL
    recordings: list[TranscriptRecording] = Field(default_factory=list)


def _read_payload(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TranscriptError(
            TranscriptErrorReason.UNREADABLE,
            f"could not read transcript {path}: {exc}",
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TranscriptError(
            TranscriptErrorReason.INVALID,
            f"invalid transcript JSON: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise TranscriptError(
            TranscriptErrorReason.INVALID,
            "transcript must be a JSON object",
        )
    return payload


def load_transcript(
    path: Path | str,
    *,
    expected_model_id: str | None = None,
) -> LLMTranscript:
    transcript_path = Path(path).expanduser().resolve()
    payload = _read_payload(transcript_path)
    version = payload.get("transcript_version")
    if version != TRANSCRIPT_VERSION:
        raise TranscriptError(
            TranscriptErrorReason.UNSUPPORTED_VERSION,
            f"unsupported transcript version {version!r}; "
            f"expected {TRANSCRIPT_VERSION}",
        )
    try:
        transcript = LLMTranscript.model_validate(payload)
    except ValidationError as exc:
        raise TranscriptError(
            TranscriptErrorReason.INVALID,
            f"invalid transcript: {exc}",
        ) from exc

    if transcript.model_kind is not ModelKind.REAL:
        raise TranscriptError(
            TranscriptErrorReason.INVALID,
            "replay transcripts must record model_kind=real",
        )
    if expected_model_id is not None and transcript.model_id != expected_model_id:
        raise ReplayError(
            ReplayErrorReason.MODEL_MISMATCH,
            f"transcript model {transcript.model_id!r} does not match "
            f"expected {expected_model_id!r}",
        )

    seen: set[str] = set()
    for recording in transcript.recordings:
        if recording.request_id in seen:
            raise TranscriptError(
                TranscriptErrorReason.DUPLICATE_REQUEST,
                f"duplicate transcript request id {recording.request_id!r}",
            )
        seen.add(recording.request_id)
        if recording.model_kind is not ModelKind.REAL:
            raise TranscriptError(
                TranscriptErrorReason.INVALID,
                f"recording {recording.request_id!r} is not a real response",
            )
        if recording.model_id != transcript.model_id:
            raise TranscriptError(
                TranscriptErrorReason.INVALID,
                f"recording {recording.request_id!r} model id "
                f"{recording.model_id!r} does not match transcript model "
                f"{transcript.model_id!r}",
            )
        if digest_text(recording.text) != recording.response_digest:
            raise ReplayError(
                ReplayErrorReason.RESPONSE_DIGEST_MISMATCH,
                f"response digest mismatch for {recording.request_id!r}",
            )
    return transcript


class ReplayLLM:
    """Replay recorded responses by request id; never touches the network."""

    replayed = True

    def __init__(
        self,
        transcript: LLMTranscript | Path | str,
        *,
        expected_model_id: str | None = None,
    ) -> None:
        if isinstance(transcript, (str, Path)):
            transcript = load_transcript(
                transcript, expected_model_id=expected_model_id
            )
        self.transcript = transcript
        self.model_kind = transcript.model_kind
        self.model_id = transcript.model_id
        self.stats = LLMCallStats(replayed=True)
        self.calls: list[LLMRequest] = []
        self._by_id = {
            recording.request_id: recording
            for recording in transcript.recordings
        }

    def complete(self, request: LLMRequest) -> LLMResponse:
        recording = self._by_id.get(request.request_id)
        if recording is None:
            raise ReplayError(
                ReplayErrorReason.MISSING_RESPONSE,
                f"no recorded response for {request.request_id!r}",
            )
        if recording.purpose != request.purpose.value:
            raise ReplayError(
                ReplayErrorReason.PURPOSE_MISMATCH,
                f"recorded purpose {recording.purpose!r} does not match "
                f"{request.purpose.value!r} for {request.request_id!r}",
            )
        if recording.prompt_template_version != request.prompt_template_version:
            raise ReplayError(
                ReplayErrorReason.TEMPLATE_VERSION_MISMATCH,
                f"recorded template {recording.prompt_template_version!r} "
                f"does not match {request.prompt_template_version!r}",
            )
        if recording.prompt_digest != request.prompt_digest:
            raise ReplayError(
                ReplayErrorReason.PROMPT_DIGEST_MISMATCH,
                f"prompt digest mismatch for {request.request_id!r}",
            )
        if recording.response_digest != digest_text(recording.text):
            raise ReplayError(
                ReplayErrorReason.RESPONSE_DIGEST_MISMATCH,
                f"response digest mismatch for {request.request_id!r}",
            )

        self.calls.append(request)
        self.stats.calls += 1
        self.stats.retries += max(0, recording.attempts - 1)
        self.stats.prompt_tokens += recording.usage.prompt_tokens or 0
        self.stats.completion_tokens += recording.usage.completion_tokens or 0
        return LLMResponse(
            request_id=request.request_id,
            text=recording.text,
            model_id=recording.model_id,
            model_kind=recording.model_kind,
            finish_reason=recording.finish_reason,
            usage=recording.usage,
            attempts=recording.attempts,
            replayed=True,
        )
