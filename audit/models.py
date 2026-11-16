"""Models for deterministic rule scanning."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from llm.models import ModelKind
from project.files import SkippedFile
from sandbox.runner import EnforcementMode


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
    llm_calls: int = 0
    llm_retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    replayed: bool = False
    candidates_considered: int = 0
    drafts_accepted: int = 0
    drafts_rejected: int = 0
    rejected_by_reason: dict[TriageRejectReason, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class TriageReport(BaseModel):
    drafts: list[TriageDraft] = Field(default_factory=list)
    rejected: list[RejectedTriage] = Field(default_factory=list)
    trace: TriageTrace


class ReproFailureReason(str, Enum):
    CLAIM_NOT_OBSERVED = "claim_not_observed"
    SANDBOX_ERROR = "sandbox_error"
    TIMEOUT = "timeout"
    FLAKY = "flaky"


class ReproSkipReason(str, Enum):
    NOT_PASSED = "not_passed"
    UNSUPPORTED_RULE = "unsupported_rule"
    UNSUPPORTED_SIGNATURE = "unsupported_signature"
    TEMPLATE_BUILD_FAILED = "template_build_failed"


class ReproArtifact(BaseModel):
    finding_id: str
    rule_id: RuleId
    file: str
    line_start: int
    line_end: int
    script_source: str
    script_path: str
    exit_code: int
    timed_out: bool
    elapsed_ms: int
    stdout_digest: str | None = None
    stderr_digest: str | None = None
    platform: str
    enforcement_mode: EnforcementMode
    double_run: bool
    reproduced: bool
    failure_reason: ReproFailureReason | None = None


class ReproSkipped(BaseModel):
    finding_id: str
    rule_id: RuleId
    file: str
    line_start: int
    line_end: int
    reason: ReproSkipReason
    detail: str | None = None


class ReproTrace(BaseModel):
    started_at: str
    elapsed_ms: int
    findings_considered: int = 0
    attempted: int = 0
    reproduced: int = 0
    skipped_by_reason: dict[ReproSkipReason, int] = Field(default_factory=dict)
    failures_by_reason: dict[ReproFailureReason, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ReproReport(BaseModel):
    artifacts: list[ReproArtifact] = Field(default_factory=list)
    skipped: list[ReproSkipped] = Field(default_factory=list)
    trace: ReproTrace


class RetractReason(str, Enum):
    REFUTED_BY_CONTEXT = "REFUTED_BY_CONTEXT"
    NO_LINE_LOCATION = "NO_LINE_LOCATION"
    UNVERIFIABLE_CLAIM = "UNVERIFIABLE_CLAIM"
    DUPLICATE = "DUPLICATE"


class EvidenceLevel(str, Enum):
    REPRODUCED = "REPRODUCED"
    STATIC_CONFIRMED = "STATIC_CONFIRMED"
    SUSPICIOUS = "SUSPICIOUS"


class Finding(BaseModel):
    finding_id: str
    file: str
    line_start: int
    line_end: int
    rule_id: RuleId | None = None
    llm_finding_id: str | None = None
    source: FindingSource
    title: str
    evidence_level: EvidenceLevel
    claim: str | None = None
    trigger_path: list[str] = Field(default_factory=list)
    repro_artifact: ReproArtifact | None = None


class Retracted(BaseModel):
    finding: Finding
    reason: RetractReason


class GradingTrace(BaseModel):
    started_at: str
    elapsed_ms: int
    candidates_considered: int = 0
    findings_emitted: int = 0
    folded_suspicious: int = 0
    retracted: int = 0
    evidence_level_counts: dict[EvidenceLevel, int] = Field(default_factory=dict)
    retract_reason_counts: dict[RetractReason, int] = Field(default_factory=dict)
    repro_attempted: int = 0
    repro_reproduced: int = 0
    warnings: list[str] = Field(default_factory=list)


class AuditReport(BaseModel):
    findings: list[Finding] = Field(default_factory=list)
    folded_suspicious: list[Finding] = Field(default_factory=list)
    retracted: list[Retracted] = Field(default_factory=list)
    trace: GradingTrace
