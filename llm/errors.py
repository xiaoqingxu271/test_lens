"""Enumerated model, transcript, replay, and budget failures."""

from __future__ import annotations

from enum import Enum


class LLMErrorReason(str, Enum):
    AUTH = "LLM_AUTH_ERROR"
    TIMEOUT = "LLM_TIMEOUT"
    RATE_LIMIT = "LLM_RATE_LIMIT"
    TRANSPORT = "LLM_TRANSPORT_ERROR"
    RESPONSE = "LLM_RESPONSE_ERROR"
    BUDGET = "LLM_BUDGET_EXCEEDED"


class TranscriptErrorReason(str, Enum):
    UNREADABLE = "transcript_unreadable"
    INVALID = "invalid_transcript"
    UNSUPPORTED_VERSION = "unsupported_transcript_version"
    DUPLICATE_REQUEST = "duplicate_request"


class ReplayErrorReason(str, Enum):
    MISSING_RESPONSE = "missing_response"
    PURPOSE_MISMATCH = "purpose_mismatch"
    PROMPT_DIGEST_MISMATCH = "prompt_digest_mismatch"
    TEMPLATE_VERSION_MISMATCH = "template_version_mismatch"
    RESPONSE_DIGEST_MISMATCH = "response_digest_mismatch"
    MODEL_MISMATCH = "model_mismatch"


class LLMError(RuntimeError):
    reason: LLMErrorReason | None = None

    def __init__(self, message: str, *, reason: LLMErrorReason | None = None):
        super().__init__(message)
        self.reason = reason


class TranscriptError(LLMError):
    transcript_reason: TranscriptErrorReason

    def __init__(self, transcript_reason: TranscriptErrorReason, message: str):
        super().__init__(message)
        self.transcript_reason = transcript_reason


class ReplayError(LLMError):
    replay_reason: ReplayErrorReason

    def __init__(self, replay_reason: ReplayErrorReason, message: str):
        super().__init__(message)
        self.replay_reason = replay_reason


class RecordingError(LLMError):
    pass


class LLMBudgetExceeded(LLMError):
    def __init__(self, message: str):
        super().__init__(message, reason=LLMErrorReason.BUDGET)
