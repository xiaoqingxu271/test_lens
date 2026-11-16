"""Stage-boundary models for the test pipeline closure.

A generated test is only a candidate. This package materializes candidates in
an isolated work directory, executes each one through the double-run sandbox,
scores it with target-local mutants, and attributes every discard with an
enumerated reason. Only ``KeptTest`` items survive into ``TestReport.kept``.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from generation.models import GeneratedTest
from mutation.score import MutationScoreReport
from sandbox.runner import (
    EnforcementMode,
    FailureKind,
    FinalStatus,
    FlakyRecord,
    RunnerError,
    TestResult,
)


class DiscardReason(str, Enum):
    SYNTAX_ERROR = "SYNTAX_ERROR"
    ENVIRONMENT_MISSING = "ENVIRONMENT_MISSING"
    RUNTIME_ERROR_ON_HAPPY = "RUNTIME_ERROR_ON_HAPPY"
    ASSERTION_TOO_WEAK = "ASSERTION_TOO_WEAK"
    TARGET_MISMATCH = "TARGET_MISMATCH"
    FLAKY = "FLAKY"


class MaterializedTest(BaseModel):
    test_id: str
    target_symbol: str
    test_file: str
    test_name: str
    path: str
    source_digest: str


class MaterializedProject(BaseModel):
    project_root: str
    artifacts_root: str
    tests: list[MaterializedTest] = Field(default_factory=list)


class KeptTest(BaseModel):
    target_symbol: str
    test_file: str
    test_name: str
    execution_status: Literal["passed"] = "passed"
    mutation_kill_rate: float
    mutations_run: int
    mutations_killed: int


class DiscardedTest(BaseModel):
    test_id: str
    target_symbol: str
    test_file: str
    test_name: str
    reason: DiscardReason
    detail: str = ""
    final_status: FinalStatus | None = None
    failure_kinds: list[FailureKind] = Field(default_factory=list)
    mutation_kill_rate: float | None = None
    mutations_run: int | None = None
    mutations_killed: int | None = None


class CandidateEvidence(BaseModel):
    """Everything attribution may look at for one generated candidate."""

    candidate: GeneratedTest
    test_result: TestResult | None = None
    flaky_record: FlakyRecord | None = None
    mutation: MutationScoreReport | None = None
    runner_errors: list[RunnerError] = Field(default_factory=list)


class TestPipelineTrace(BaseModel):
    started_at: str
    elapsed_ms: int
    candidates_in: int = 0
    tests_materialized: int = 0
    tests_executed: int = 0
    sandbox_passed: int = 0
    flaky: int = 0
    mutation_scored: int = 0
    mutations_run: int = 0
    mutations_killed: int = 0
    killed_by_assertion: int = 0
    killed_by_exception: int = 0
    killed_by_timeout: int = 0
    kept: int = 0
    discarded_by_reason: dict[DiscardReason, int] = Field(default_factory=dict)
    double_run: bool = True
    kill_threshold: float = 0.5
    operator_ids: list[str] = Field(default_factory=list)
    seed: int = 0
    platform: str = ""
    enforcement_mode: EnforcementMode = EnforcementMode.BEST_EFFORT
    warnings: list[str] = Field(default_factory=list)


class TestReport(BaseModel):
    kept: list[KeptTest] = Field(default_factory=list)
    discarded: list[DiscardedTest] = Field(default_factory=list)
    flaky: list[FlakyRecord] = Field(default_factory=list)
    trace: TestPipelineTrace
