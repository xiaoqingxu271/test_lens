"""Record real model responses into a replayable transcript."""

from __future__ import annotations

import json
from pathlib import Path

from .errors import RecordingError
from .models import LLMCallStats, LLMRequest, LLMResponse, ModelKind
from .replay import (
    TRANSCRIPT_VERSION,
    LLMTranscript,
    TranscriptRecording,
)


class RecordingLLM:
    """Wrap a real client and atomically append each response to a transcript."""

    def __init__(
        self,
        inner,
        path: Path | str,
        *,
        overwrite: bool = False,
    ) -> None:
        self._inner = inner
        self.path = Path(path).expanduser().resolve()
        self.model_kind = inner.model_kind
        self.model_id = inner.model_id
        self._recordings: list[TranscriptRecording] = []
        if self.path.exists() and not overwrite:
            raise RecordingError(
                f"transcript already exists: {self.path}; "
                "pass overwrite=True to replace it"
            )
        self._write()

    @property
    def stats(self) -> LLMCallStats:
        stats = getattr(self._inner, "stats", None)
        return stats if isinstance(stats, LLMCallStats) else LLMCallStats()

    def complete(self, request: LLMRequest) -> LLMResponse:
        response = self._inner.complete(request)
        if response.request_id != request.request_id:
            raise RecordingError(
                f"response id {response.request_id!r} does not match "
                f"request id {request.request_id!r}"
            )
        if response.model_kind is not ModelKind.REAL:
            raise RecordingError(
                "only real model responses can be recorded; "
                f"got model_kind={response.model_kind.value}"
            )
        self._recordings.append(
            TranscriptRecording(
                request_id=request.request_id,
                purpose=request.purpose.value,
                prompt_template_version=request.prompt_template_version,
                prompt_digest=request.prompt_digest,
                response_digest=response.response_digest,
                text=response.text,
                model_id=response.model_id,
                model_kind=response.model_kind,
                finish_reason=response.finish_reason,
                attempts=response.attempts,
                usage=response.usage,
            )
        )
        self._write()
        return response

    def _write(self) -> None:
        transcript = LLMTranscript(
            transcript_version=TRANSCRIPT_VERSION,
            created_by="testlens",
            model_id=self.model_id,
            model_kind=ModelKind.REAL,
            recordings=sorted(
                self._recordings, key=lambda item: item.request_id
            ),
        )
        payload = json.dumps(
            transcript.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
        )
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError as exc:
            raise RecordingError(
                f"could not write transcript {self.path}: {exc}"
            ) from exc
