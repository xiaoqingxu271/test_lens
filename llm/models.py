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
    LLM_INDEPENDENT = "llm_independent"


class ModelKind(str, Enum):
    FAKE = "fake"
    REAL = "real"


class FinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    REFUSAL = "refusal"
    EMPTY = "empty"
    UNKNOWN = "unknown"


class LLMUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class LLMCallStats(BaseModel):
    calls: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    replayed: bool = False


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
    attempts: int = 1
    replayed: bool = False

    @computed_field
    @property
    def response_digest(self) -> str:
        return digest_text(self.text)


def collect_stats(client: object) -> LLMCallStats:
    """Snapshot a client's cumulative call stats.

    Clients shared across stages (replay and real runs) keep cumulative
    counters, so a stage must diff two snapshots to report its own usage.
    """

    stats = getattr(client, "stats", None)
    if isinstance(stats, LLMCallStats):
        return stats.model_copy(deep=True)
    return LLMCallStats()


def stats_delta(before: LLMCallStats, after: LLMCallStats) -> LLMCallStats:
    """Per-stage usage from two cumulative snapshots."""
    return LLMCallStats(
        calls=max(after.calls - before.calls, 0),
        retries=max(after.retries - before.retries, 0),
        prompt_tokens=max(after.prompt_tokens - before.prompt_tokens, 0),
        completion_tokens=max(
            after.completion_tokens - before.completion_tokens, 0
        ),
        replayed=after.replayed,
    )
