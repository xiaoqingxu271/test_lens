"""Models for deterministic rule scanning."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

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
