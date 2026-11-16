"""Provider-neutral LLM request/response contracts.

Every stage talks to a model through these models only. The request and
response digests are computed, never supplied by a client, so a report can be
reproduced without storing prompts or completions verbatim.
"""

from __future__ import annotations

import hashlib
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, computed_field


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class LLMPurpose(str, Enum):
    TEST_GENERATION = "test_generation"
    RULE_TRIAGE = "rule_triage"


class ModelKind(str, Enum):
    FAKE = "fake"
    REAL = "real"


class FinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    REFUSAL = "refusal"
    EMPTY = "empty"


class LLMUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class LLMRequest(BaseModel):
    """A single completion request.

    ``request_id`` is deterministic per (purpose, subject) so fake responses
    and trace entries are stable across runs. ``prompt_digest`` covers the
    template version, system prompt, and user prompt.
    """

    model_config = ConfigDict(frozen=True)

    request_id: str
    purpose: LLMPurpose
    prompt_template_version: str
    system: str
    prompt: str
    seed: int = 0
    temperature: float = 0.0

    @computed_field
    @property
    def prompt_digest(self) -> str:
        return digest_text(
            "\x1f".join((self.prompt_template_version, self.system, self.prompt))
        )


class LLMResponse(BaseModel):
    request_id: str
    text: str
    model_id: str
    model_kind: ModelKind
    finish_reason: FinishReason = FinishReason.STOP
    usage: LLMUsage = Field(default_factory=LLMUsage)

    @computed_field
    @property
    def response_digest(self) -> str:
        return digest_text(self.text)
