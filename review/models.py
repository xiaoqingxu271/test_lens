"""Stage-boundary models for the false-positive cross-check."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from audit.models import FindingSource, RetractReason, RuleId


class ReviewVerdict(str, Enum):
    PASS = "pass"
    REFUTE = "refute"
    UNVERIFIABLE = "unverifiable"


class EvidenceKind(str, Enum):
    GUARD = "guard"
    UNREACHABLE = "unreachable"
    TYPE_CONSTRAINT = "type_constraint"
    DUPLICATE = "duplicate"
    REPRODUCTION = "reproduction"
    RULE = "rule"
    DATA_FLOW = "data_flow"


class EvidenceRole(str, Enum):
    SUPPORTS = "supports"
    REFUTES = "refutes"


class ReviewerKind(str, Enum):
    RULE = "rule"
    MODEL = "model"
    FAKE = "fake"


class EvidenceLine(BaseModel):
    file: str
    line_start: int
    line_end: int
    evidence_kind: EvidenceKind
    role: EvidenceRole
    explanation: str
    snippet_hash: str
    snippet: str | None = None


class CrossCheckSubject(BaseModel):
    """A claim handed to cross-check.

    ``rule_id`` is set for rule-backed drafts; semantic claims (v2) carry
    ``rule_id=None`` and are reviewed independently.
    """

    finding_id: str
    rule_id: RuleId | None = None
    source: FindingSource = FindingSource.RULE_WITH_LLM_TRIAGE
    file: str
    line_start: int
    line_end: int
    claim: str
    trigger_path: list[str] = Field(default_factory=list)


class EnclosingFunction(BaseModel):
    name: str
    line_start: int
    line_end: int
    source: str


class GuardRef(BaseModel):
    line: int
    source: str


class CallerRef(BaseModel):
    name: str
    line: int


class OtherRuleHit(BaseModel):
    finding_id: str
    rule_id: RuleId
    line_start: int
    line_end: int
    title: str
    message: str


class ReviewContext(BaseModel):
    file: str
    file_line_count: int
    imports: list[str] = Field(default_factory=list)
    enclosing_function: EnclosingFunction | None = None
    routes: list[str] = Field(default_factory=list)
    callers: list[CallerRef] = Field(default_factory=list)
    type_hints: list[str] = Field(default_factory=list)
    guards: list[GuardRef] = Field(default_factory=list)
    other_rule_hits: list[OtherRuleHit] = Field(default_factory=list)
    source_context_hash: str = ""


class ReviewRequest(BaseModel):
    request_id: str
    finding_id: str
    rule_id: RuleId | None
    file: str
    line_start: int
    line_end: int
    claim: str
    trigger_path: list[str] = Field(default_factory=list)
    subject_snippet: str
    subject_snippet_hash: str
    context: ReviewContext


class ReviewerVerdict(BaseModel):
    verdict: ReviewVerdict
    evidence_lines: list[EvidenceLine] = Field(default_factory=list)
    retract_reason: RetractReason | None = None
    checked_rule_ids: list[str] = Field(default_factory=list)


class CrossCheckResult(BaseModel):
    finding_id: str
    claim_hash: str
    verdict: ReviewVerdict
    evidence_lines: list[EvidenceLine] = Field(default_factory=list)
    retract_reason: RetractReason | None = None
    checked_rule_ids: list[str] = Field(default_factory=list)
    reviewer_kind: ReviewerKind
    reviewer_id: str
    source_context_hash: str
    elapsed_ms: int


class CrossCheckTrace(BaseModel):
    started_at: str
    elapsed_ms: int
    subjects_considered: int = 0
    verdict_counts: dict[ReviewVerdict, int] = Field(default_factory=dict)
    rule_checks: int = 0
    model_reviews: int = 0
    reviewer_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CrossCheckReport(BaseModel):
    results: list[CrossCheckResult] = Field(default_factory=list)
    trace: CrossCheckTrace
