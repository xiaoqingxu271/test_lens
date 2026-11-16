"""Models for deterministic rule scanning."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from llm.models import ModelKind
from project.files import SkippedFile


class RuleId(str, Enum):
    R001_MUTABLE_DEFAULT = "R001-MUTABLE-DEFAULT"
    R002_NONE_DEREF_NO_GUARD = "R002-NONE-DEREF-NO-GUARD"
    R003_UNREACHABLE_AFTER_RETURN = "R003-UNREACHABLE-AFTER-RETURN"
    R004_BARE_EXCEPT_SWALLOW = "R004-BARE-EXCEPT-SWALLOW"
    R005_RESOURCE_NO_WITH = "R005-RESOURCE-NO-WITH"


class FindingSource(str, Enum):
    RULE = "RULE"
    RULE_WITH_LLM_TRIAGE = "RULE_WITH_LLM_TRIAGE"
    LLM_INDEPENDENT = "LLM_INDEPENDENT"  # reserved for v2; never emitted in v1


class RuleCandidate(BaseModel):
    finding_id: str
    rule_id: RuleId
    file: str
    relative_file: str
    line_start: int
    line_end: int
    column: int
    title: str
    message: str
    snippet: str
    snippet_hash: str
    source: FindingSource = FindingSource.RULE


class RuleScanTrace(BaseModel):
    started_at: str
    elapsed_ms: int
    files_scanned: int
    files_skipped: list[SkippedFile] = Field(default_factory=list)
    candidates_found: int = 0
    candidates_by_rule: dict[RuleId, int] = Field(default_factory=dict)
    rules_run: list[RuleId] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RuleScanReport(BaseModel):
    candidates: list[RuleCandidate] = Field(default_factory=list)
    trace: RuleScanTrace


class TriageRejectReason(str, Enum):
    EMPTY_RESPONSE = "empty_response"
    INVALID_JSON = "invalid_json"
    INVALID_SHAPE = "invalid_shape"
    SOURCE_UNREADABLE = "source_unreadable"
    UNKNOWN_FINDING = "unknown_finding"
    RULE_MISMATCH = "rule_mismatch"
    LINE_ANCHOR_MISMATCH = "line_anchor_mismatch"
    NO_TRIGGER_PATH = "no_trigger_path"
    EMPTY_CLAIM = "empty_claim"


class TriageDraft(BaseModel):
    """A rule candidate whose trigger path was completed by LLM triage.

    Line anchors and rule identity are copied from the deterministic
    candidate; triage cannot move or reclassify a finding.
    """

    finding_id: str
    rule_id: RuleId
    source: FindingSource = FindingSource.RULE_WITH_LLM_TRIAGE
    file: str
    line_start: int
    line_end: int
    claim: str
    trigger_path: list[str]
    prompt_digest: str
    response_digest: str
    prompt_template_version: str
    model_kind: ModelKind
    model_id: str


class RejectedTriage(BaseModel):
    finding_id: str
    rule_id: RuleId
    file: str
    reason: TriageRejectReason
    detail: str
    prompt_digest: str = ""
    response_digest: str = ""
    model_kind: ModelKind = ModelKind.FAKE
    model_id: str = ""


class TriageTrace(BaseModel):
    started_at: str
    elapsed_ms: int
    llm_model_kind: ModelKind
    llm_model_id: str
    prompt_template_version: str
    candidates_considered: int = 0
    drafts_accepted: int = 0
    drafts_rejected: int = 0
    rejected_by_reason: dict[TriageRejectReason, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class TriageReport(BaseModel):
    drafts: list[TriageDraft] = Field(default_factory=list)
    rejected: list[RejectedTriage] = Field(default_factory=list)
    trace: TriageTrace
