"""Evaluation report models.

The evaluation harness is a reader of stage reports, not a new pipeline. It
records only deterministic metrics and compact finding summaries so a report
can be compared across runs without depending on temporary paths.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from llm.models import ModelKind
from sandbox.runner import EnforcementMode


class Label(BaseModel):
    file: str
    rule_id: str
    marker: str
    line: int
    case: str | None = None
    retract_reason: str | None = None


class EvaluationLabels(BaseModel):
    project: str
    defects: list[Label] = Field(default_factory=list)
    false_positives: list[Label] = Field(default_factory=list)
    false_negatives: list[Label] = Field(default_factory=list)


class FindingSummary(BaseModel):
    file: str
    line_start: int
    line_end: int
    rule_id: str | None = None
    source: str
    evidence_level: str


class RuleEvaluation(BaseModel):
    rule_id: str
    labeled_defects: int = 0
    defects_found: int = 0
    emitted_findings: int = 0
    true_positives: int = 0
    false_positives: int = 0


class TestSideEvaluation(BaseModel):
    candidates: int = 0
    kept: int = 0
    discarded_by_reason: dict[str, int] = Field(default_factory=dict)
    flaky: int = 0
    flaky_rate: float = 0.0
    mutation_scored: int = 0
    mutations_run: int = 0
    mutations_killed: int = 0
    killed_by_assertion: int = 0
    killed_by_exception: int = 0
    killed_by_timeout: int = 0
    mean_kill_rate: float | None = None
    mean_kill_rate_scored: float | None = None
    mutation_kill_rates: list[float] = Field(default_factory=list)
    unscored_discarded_by_reason: dict[str, int] = Field(default_factory=dict)
    double_run: bool = True
    platform: str = ""
    enforcement_mode: EnforcementMode = EnforcementMode.BEST_EFFORT
    seed: int = 0
    threshold: float = 0.5
    operator_set_version: str = ""
    equivalent_mutants_undetected: bool = True


class AuditSideEvaluation(BaseModel):
    candidates: int = 0
    findings_emitted: int = 0
    defects_labeled: int = 0
    defects_found: int = 0
    recall: float | None = None
    true_positives: int = 0
    false_positives: int = 0
    precision: float | None = None
    false_positive_labels: int = 0
    false_positives_retracted: int = 0
    false_negative_labels: int = 0
    false_negatives_observed: int = 0
    evidence_level_counts: dict[str, int] = Field(default_factory=dict)
    retract_reason_counts: dict[str, int] = Field(default_factory=dict)
    per_rule: list[RuleEvaluation] = Field(default_factory=list)
    findings: list[FindingSummary] = Field(default_factory=list)


class FixtureEvaluation(BaseModel):
    project: str
    test: TestSideEvaluation
    audit: AuditSideEvaluation


class CalibrationRow(BaseModel):
    project: str
    threshold: float
    kept: int
    discarded_by_reason: dict[str, int] = Field(default_factory=dict)


class EvaluationTrace(BaseModel):
    started_at: str
    elapsed_ms: int
    fixtures: list[str] = Field(default_factory=list)
    model_kind: ModelKind = ModelKind.FAKE
    model_id: str = ""
    prompt_template_version: str = ""
    seed: int = 0
    threshold: float = 0.5
    calibration_thresholds: list[float] = Field(default_factory=list)
    double_run: bool = True
    platform: str = ""
    enforcement_mode: EnforcementMode = EnforcementMode.BEST_EFFORT
    sandbox_ready: bool = False
    warnings: list[str] = Field(default_factory=list)


class EvaluationReport(BaseModel):
    fixtures: list[FixtureEvaluation] = Field(default_factory=list)
    calibration: list[CalibrationRow] = Field(default_factory=list)
    trace: EvaluationTrace
    output_dir: str | None = None
