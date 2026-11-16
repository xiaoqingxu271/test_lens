from __future__ import annotations

from pathlib import Path

import evaluation
import llm
from audit.models import (
    AuditReport,
    EvidenceLevel,
    Finding,
    FindingSource,
    Retracted,
    RetractReason,
)
from evaluation.labels import fixture_roots
from evaluation.run import DEFAULT_FIXTURES
from llm.models import ModelKind
from mutation.score import (
    MutantResult,
    MutationScoreReport,
    MutationTrace,
)
from pipeline.models import (
    DiscardReason,
    DiscardedTest,
    KeptTest,
    TestReport as PipelineReport,
)
from review.models import CrossCheckResult, EvidenceLine
from sandbox.container import ContainerRuntime, SandboxBackend
from sandbox.runner import (
    EnforcementMode,
    ExecutionReport,
    ExecutionTrace,
    FailureDetail,
    FailureKind,
    FinalStatus,
    FlakyRecord,
    ResourceSummary,
    ResourceUsage,
    RunnerError,
    SandboxConfig,
    SymbolCoverage,
    TestOutcome as Outcome,
    TestResult as Result,
    TestRunResult as RunResult,
    TimeoutInfo,
    TimeoutSummary,
)

CONTRACTS = {
    ExecutionReport: {
        "run_id",
        "started_at",
        "elapsed_ms",
        "project_profile_id",
        "sandbox_config",
        "runs",
        "tests",
        "flaky",
        "resource_summary",
        "timeout_summary",
        "trace",
        "runner_errors",
    },
    SandboxConfig: {
        "timeout_seconds",
        "memory_limit_mb",
        "cpu_limit_seconds",
        "double_run",
        "enforcement_mode",
        "platform",
        "coverage_source",
        "backend",
        "container",
    },
    RunResult: {
        "run_index",
        "started_at",
        "elapsed_ms",
        "exit_code",
        "timed_out",
        "tests",
        "resource_usage",
        "stdout_artifact",
        "stderr_artifact",
    },
    Outcome: {
        "test_id",
        "test_file",
        "test_name",
        "status",
        "failure_kind",
        "exception_type",
        "message_digest",
        "duration_ms",
        "covered_target_symbols",
        "timeout",
        "resource_usage",
        "stdout_artifact",
        "stderr_artifact",
    },
    Result: {
        "test_id",
        "test_file",
        "test_name",
        "target_symbol",
        "final_status",
        "runs",
        "flaky",
        "failure_kinds",
        "covered_target_symbols_union",
        "timeout",
        "resource_usage",
    },
    SymbolCoverage: {
        "symbol_id",
        "file",
        "line_start",
        "line_end",
        "entry_hit",
        "executed_lines",
        "coverage_source",
    },
    FlakyRecord: {
        "test_id",
        "test_file",
        "test_name",
        "run_statuses",
        "consistent",
        "failure_details_by_run",
    },
    FailureDetail: {
        "run_index",
        "failure_kind",
        "exception_type",
        "message_digest",
    },
    TimeoutInfo: {
        "timed_out",
        "timeout_seconds",
        "phase",
        "process_tree_killed",
    },
    ResourceUsage: {
        "wall_time_ms",
        "cpu_time_seconds",
        "peak_rss_mb",
        "limits_hit",
        "enforcement_mode",
    },
    ResourceSummary: {
        "peak_rss_mb",
        "cpu_time_seconds",
        "wall_time_ms",
        "limits_hit",
        "enforcement_mode",
        "platform",
    },
    TimeoutSummary: {
        "any_timeout",
        "timed_out_test_ids",
        "timeout_seconds",
    },
    ExecutionTrace: {
        "double_run",
        "runner_version",
        "python_version",
        "cwd",
        "env_digest",
        "backend",
        "warnings",
    },
    RunnerError: {"stage", "message", "fatal"},
    MutationScoreReport: {
        "target_symbol",
        "test_id",
        "operator_set_version",
        "seed",
        "mutants_total",
        "mutations_run",
        "mutations_killed",
        "kill_rate",
        "killed_by_assertion",
        "killed_by_exception",
        "killed_by_timeout",
        "survived",
        "mutant_ids",
        "equivalent_mutants_undetected",
        "mutant_results",
        "trace",
    },
    MutationTrace: {
        "started_at",
        "elapsed_ms",
        "operator_ids",
        "mutant_generation_digest",
        "killed_by_assertion",
        "killed_by_exception",
        "killed_by_timeout",
        "warnings",
    },
    MutantResult: {
        "mutant_id",
        "operator_id",
        "file",
        "line",
        "status",
        "killed_by",
    },
    CrossCheckResult: {
        "finding_id",
        "claim_hash",
        "verdict",
        "evidence_lines",
        "retract_reason",
        "checked_rule_ids",
        "reviewer_kind",
        "reviewer_id",
        "source_context_hash",
        "elapsed_ms",
    },
    EvidenceLine: {
        "file",
        "line_start",
        "line_end",
        "evidence_kind",
        "role",
        "explanation",
        "snippet_hash",
        "snippet",
    },
    PipelineReport: {"kept", "discarded", "flaky", "trace"},
    KeptTest: {
        "target_symbol",
        "test_file",
        "test_name",
        "execution_status",
        "mutation_kill_rate",
        "mutations_run",
        "mutations_killed",
    },
    DiscardedTest: {
        "test_id",
        "target_symbol",
        "test_file",
        "test_name",
        "reason",
        "detail",
        "final_status",
        "failure_kinds",
        "mutation_kill_rate",
        "mutations_run",
        "mutations_killed",
    },
    AuditReport: {"findings", "folded_suspicious", "retracted", "trace"},
    Finding: {
        "finding_id",
        "file",
        "line_start",
        "line_end",
        "rule_id",
        "llm_finding_id",
        "source",
        "title",
        "evidence_level",
        "claim",
        "trigger_path",
        "repro_artifact",
    },
    Retracted: {"finding", "reason"},
}


def test_report_models_keep_every_contract_field():
    for model, required in CONTRACTS.items():
        fields = set(model.model_fields)
        missing = required - fields
        assert not missing, f"{model.__name__} is missing {sorted(missing)}"


def test_contract_enums_match_the_specification():
    assert {item.value for item in FinalStatus} == {
        "passed",
        "failed",
        "error",
        "timeout",
        "skipped",
        "flaky",
    }
    assert {item.value for item in FailureKind} == {
        "none",
        "assertion",
        "exception",
        "import_error",
        "collection_error",
        "timeout",
        "runner_error",
    }
    assert {item.value for item in DiscardReason} == {
        "SYNTAX_ERROR",
        "ENVIRONMENT_MISSING",
        "RUNTIME_ERROR_ON_HAPPY",
        "ASSERTION_TOO_WEAK",
        "TARGET_MISMATCH",
        "FLAKY",
    }
    assert {item.value for item in EvidenceLevel} == {
        "REPRODUCED",
        "STATIC_CONFIRMED",
        "SUSPICIOUS",
    }
    assert {item.value for item in RetractReason} == {
        "REFUTED_BY_CONTEXT",
        "NO_LINE_LOCATION",
        "UNVERIFIABLE_CLAIM",
        "DUPLICATE",
    }
    assert {item.value for item in FindingSource} == {
        "RULE",
        "RULE_WITH_LLM_TRIAGE",
        "LLM_INDEPENDENT",
    }
    assert {item.value for item in ModelKind} == {"fake", "real"}
    assert {item.value for item in EnforcementMode} == {
        "enforced",
        "best-effort",
    }
    assert {item.value for item in SandboxBackend} == {
        "subprocess",
        "container",
    }
    assert {item.value for item in ContainerRuntime} == {"docker"}


def test_evaluation_fixtures_are_packaged_with_the_module():
    package_root = Path(evaluation.__file__).resolve().parent
    assert DEFAULT_FIXTURES == package_root / "fixtures"
    assert DEFAULT_FIXTURES.is_dir()
    assert [root.name for root in fixture_roots(DEFAULT_FIXTURES)] == [
        "fastapi_project",
        "flask_project",
    ]


def test_only_the_provider_adapter_imports_network_clients():
    roots = [
        Path(llm.__file__).resolve().parent,
        Path(__file__).resolve().parents[1] / "cli",
    ]
    forbidden = (
        "import requests",
        "import httpx",
        "import openai",
        "import anthropic",
        "urllib.request",
        "socket.socket",
    )
    allowed = {Path(llm.__file__).resolve().parent / "real.py"}
    for root in roots:
        for path in root.glob("*.py"):
            if path in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in text, f"{path.name} imports {token}"


def test_llm_contract_additions_are_optional_and_defaulted():
    from llm.models import FinishReason, LLMRequest, LLMResponse

    assert {"attempts", "replayed"} <= set(LLMResponse.model_fields)
    assert LLMResponse.model_fields["attempts"].default == 1
    assert LLMResponse.model_fields["replayed"].default is False
    assert FinishReason.UNKNOWN.value == "unknown"
    assert {
        "request_id",
        "purpose",
        "prompt_template_version",
        "system",
        "prompt",
        "seed",
        "temperature",
    } <= set(LLMRequest.model_fields)
