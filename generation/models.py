"""Stage-boundary models for candidate test generation."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from llm.models import ModelKind
from sandbox.health import SandboxHealth


class GenerationRejectReason(str, Enum):
    EMPTY_RESPONSE = "empty_response"
    NO_PROPOSAL = "no_proposal"
    INVALID_JSON = "invalid_json"
    INVALID_SHAPE = "invalid_shape"
    SOURCE_UNREADABLE = "source_unreadable"
    INVALID_TEST_PATH = "invalid_test_path"
    FILE_EXISTS = "file_exists"
    SYNTAX_ERROR = "syntax_error"
    NO_TEST_FUNCTION = "no_test_function"
    TARGET_MISMATCH = "target_mismatch"
    DUPLICATE_TEST_ID = "duplicate_test_id"


class GeneratedTest(BaseModel):
    """A validated candidate; not yet executed and not yet kept."""

    test_id: str
    target_symbol: str
    test_file: str
    test_name: str
    source: str
    prompt_digest: str
    response_digest: str
    prompt_template_version: str
    model_kind: ModelKind
    model_id: str


class RejectedProposal(BaseModel):
    target_symbol: str
    reason: GenerationRejectReason
    detail: str
    prompt_digest: str = ""
    response_digest: str = ""
    model_kind: ModelKind = ModelKind.FAKE
    model_id: str = ""
    test_file: str | None = None
    test_name: str | None = None


class GenerationTrace(BaseModel):
    started_at: str
    elapsed_ms: int
    llm_model_kind: ModelKind
    llm_model_id: str
    prompt_template_version: str
    llm_calls: int = 0
    llm_retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    replayed: bool = False
    targets_considered: int = 0
    tests_proposed: int = 0
    tests_accepted: int = 0
    tests_rejected: int = 0
    rejected_by_reason: dict[GenerationRejectReason, int] = Field(default_factory=dict)
    sandbox_health: SandboxHealth
    warnings: list[str] = Field(default_factory=list)


class GenerationReport(BaseModel):
    candidates: list[GeneratedTest] = Field(default_factory=list)
    rejected: list[RejectedProposal] = Field(default_factory=list)
    trace: GenerationTrace
