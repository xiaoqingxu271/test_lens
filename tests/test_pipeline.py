from __future__ import annotations

import json
from pathlib import Path

import pytest

from generation.generate import generate_tests, generation_request_id
from generation.models import GeneratedTest
from helpers import make_root_profile, sandbox_health
from llm.fake import DeterministicFakeLLM
from llm.models import ModelKind
from mutation.operator import OperatorId
from mutation.score import MutationScoreReport, MutationTrace
from pipeline.attribute import attribute_discard
from pipeline.materialize import PipelineError, materialize_tests
from pipeline.models import (
    CandidateEvidence,
    DiscardReason,
    TestReport as PipelineReport,
)
from pipeline.run import run_test_pipeline
from sandbox.runner import (
    CoverageSource,
    FailureKind,
    FinalStatus,
    RunnerError,
    RunnerStage,
    SandboxConfig,
    SymbolCoverage,
    TestOutcome as Outcome,
    TestResult as Result,
    TestStatus as Status,
)
from targets.selection import select_targets

PIPELINE_ROOT = Path(__file__).parent / "fixtures" / "pipeline_project"
SYMBOL = "app.py::classify"
TIMESTAMP = "2026-01-01T00:00:00+00:00"
OPERATORS = (
    OperatorId.ROR_GT_TO_GTE,
    OperatorId.ROR_EQ_TO_NE,
    OperatorId.CR_NUM_0_TO_1,
)


def _snapshot(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if "__pycache__" not in path.parts
    }


def _candidate(
    name: str,
    *,
    test_file: str | None = None,
    target_symbol: str = SYMBOL,
) -> GeneratedTest:
    path = test_file or f"generated/test_{name}.py"
    return GeneratedTest(
        test_id=f"{path}::{name}",
        target_symbol=target_symbol,
        test_file=path,
        test_name=name,
        source=(
            "from app import classify\n\n\n"
            f"def {name}():\n    assert callable(classify)\n"
        ),
        prompt_digest="prompt",
        response_digest="response",
        prompt_template_version="generation-v1",
        model_kind=ModelKind.FAKE,
        model_id="fake",
    )


def _outcome(
    candidate: GeneratedTest,
    status: Status,
    kind: FailureKind = FailureKind.NONE,
    *,
    covered: bool = True,
) -> Outcome:
    coverage = (
        [
            SymbolCoverage(
                symbol_id=candidate.target_symbol,
                file="app.py",
                line_start=9,
                line_end=14,
                entry_hit=True,
                executed_lines=[10, 11],
                coverage_source=CoverageSource.SYS_SETTRACE,
            )
        ]
        if covered
        else []
    )
    return Outcome(
        test_id=candidate.test_id,
        test_file=candidate.test_file,
        test_name=candidate.test_name,
        status=status,
        failure_kind=kind,
        covered_target_symbols=coverage,
    )


def _result(
    candidate: GeneratedTest,
    final_status: FinalStatus,
    kinds: tuple[FailureKind, ...] = (FailureKind.NONE,),
    *,
    covered: bool = True,
) -> Result:
    if final_status == FinalStatus.FLAKY:
        runs = [
            _outcome(candidate, Status.FAILED, FailureKind.ASSERTION, covered=covered),
            _outcome(candidate, Status.PASSED, covered=covered),
        ]
    else:
        status = Status(final_status.value)
        kind = next(
            (item for item in kinds if item != FailureKind.NONE),
            FailureKind.NONE,
        )
        runs = [_outcome(candidate, status, kind, covered=covered)]
    return Result(
        test_id=candidate.test_id,
        test_file=candidate.test_file,
        test_name=candidate.test_name,
        target_symbol=candidate.target_symbol if covered else None,
        final_status=final_status,
        runs=runs,
        flaky=final_status == FinalStatus.FLAKY,
        failure_kinds=list(kinds),
        covered_target_symbols_union=(
            [candidate.target_symbol] if covered else []
        ),
    )


def _mutation(
    candidate: GeneratedTest,
    *,
    mutations_run: int = 5,
    mutations_killed: int = 5,
) -> MutationScoreReport:
    kill_rate = (
        mutations_killed / mutations_run if mutations_run else 0.0
    )
    return MutationScoreReport(
        target_symbol=candidate.target_symbol,
        test_id=candidate.test_id,
        mutants_total=mutations_run,
        mutations_run=mutations_run,
        mutations_killed=mutations_killed,
        kill_rate=kill_rate,
        killed_by_assertion=mutations_killed,
        killed_by_exception=0,
        killed_by_timeout=0,
        survived=mutations_run - mutations_killed,
        mutant_ids=[f"m{index}" for index in range(mutations_run)],
        trace=MutationTrace(
            started_at=TIMESTAMP,
            elapsed_ms=0,
            operator_ids=[],
            mutant_generation_digest="digest",
        ),
    )


def _evidence(
    candidate: GeneratedTest,
    result: Result | None,
    *,
    mutation: MutationScoreReport | None = None,
    errors: tuple[RunnerError, ...] = (),
) -> CandidateEvidence:
    return CandidateEvidence(
        candidate=candidate,
        test_result=result,
        mutation=mutation,
        runner_errors=list(errors),
    )


def test_attribute_discard_maps_every_discard_reason():
    strong = _candidate("test_strong")
    weak = _candidate("test_weak")
    mismatch = _candidate("test_mismatch")
    failing = _candidate("test_failing")
    broken = _candidate("test_broken")
    flaky = _candidate("test_flaky")
    missing = _candidate("test_missing")

    result = attribute_discard(
        [
            _evidence(
                strong,
                _result(strong, FinalStatus.PASSED),
                mutation=_mutation(strong),
            ),
            _evidence(
                weak,
                _result(weak, FinalStatus.PASSED),
                mutation=_mutation(weak, mutations_killed=0),
            ),
            _evidence(
                mismatch,
                _result(mismatch, FinalStatus.PASSED, covered=False),
            ),
            _evidence(
                failing,
                _result(failing, FinalStatus.FAILED, (FailureKind.ASSERTION,)),
            ),
            _evidence(
                broken,
                _result(broken, FinalStatus.ERROR, (FailureKind.IMPORT_ERROR,)),
            ),
            _evidence(flaky, _result(flaky, FinalStatus.FLAKY)),
            _evidence(
                missing,
                None,
                errors=(
                    RunnerError(
                        stage=RunnerStage.COLLECTION,
                        message="import boom",
                    ),
                ),
            ),
        ],
        kill_threshold=0.5,
    )

    assert [item.test_name for item in result.kept] == ["test_strong"]
    assert result.kept[0].execution_status == "passed"
    assert result.kept[0].mutation_kill_rate == 1.0
    assert result.kept[0].mutations_run == 5
    assert result.kept[0].mutations_killed == 5

    reasons = {item.test_name: item.reason for item in result.discarded}
    assert reasons == {
        "test_weak": DiscardReason.ASSERTION_TOO_WEAK,
        "test_mismatch": DiscardReason.TARGET_MISMATCH,
        "test_failing": DiscardReason.RUNTIME_ERROR_ON_HAPPY,
        "test_broken": DiscardReason.ENVIRONMENT_MISSING,
        "test_flaky": DiscardReason.FLAKY,
        "test_missing": DiscardReason.ENVIRONMENT_MISSING,
    }
    assert result.discarded_by_reason == {
        DiscardReason.ASSERTION_TOO_WEAK: 1,
        DiscardReason.TARGET_MISMATCH: 1,
        DiscardReason.RUNTIME_ERROR_ON_HAPPY: 1,
        DiscardReason.ENVIRONMENT_MISSING: 2,
        DiscardReason.FLAKY: 1,
    }

    assert len(result.flaky) == 1
    assert result.flaky[0].test_name == "test_flaky"
    assert len(result.flaky[0].run_statuses) == 2

    weak_discard = next(
        item for item in result.discarded if item.test_name == "test_weak"
    )
    assert weak_discard.mutation_kill_rate == 0.0
    assert weak_discard.mutations_run == 5
    assert "below threshold" in weak_discard.detail

    broken_discard = next(
        item for item in result.discarded if item.test_name == "test_broken"
    )
    assert broken_discard.failure_kinds == [FailureKind.IMPORT_ERROR]

    missing_discard = next(
        item for item in result.discarded if item.test_name == "test_missing"
    )
    assert "import boom" in missing_discard.detail


def test_attribution_threshold_is_configurable():
    weak = _candidate("test_weak")
    evidence = [
        _evidence(
            weak,
            _result(weak, FinalStatus.PASSED),
            mutation=_mutation(weak, mutations_killed=0),
        )
    ]

    strict = attribute_discard(evidence, kill_threshold=0.5)
    assert strict.kept == []
    assert strict.discarded[0].reason == DiscardReason.ASSERTION_TOO_WEAK

    permissive = attribute_discard(evidence, kill_threshold=0.0)
    assert [item.test_name for item in permissive.kept] == ["test_weak"]
    assert permissive.discarded == []


def test_passed_candidate_without_mutation_score_is_a_pipeline_error():
    strong = _candidate("test_strong")

    with pytest.raises(PipelineError):
        attribute_discard(
            [_evidence(strong, _result(strong, FinalStatus.PASSED))],
            kill_threshold=0.5,
        )


def test_duplicate_candidate_evidence_is_a_pipeline_error():
    strong = _candidate("test_strong")
    evidence = _evidence(
        strong,
        _result(strong, FinalStatus.PASSED),
        mutation=_mutation(strong),
    )

    with pytest.raises(PipelineError):
        attribute_discard([evidence, evidence], kill_threshold=0.5)


def test_materialize_writes_only_into_the_work_directory(tmp_path):
    profile = make_root_profile(PIPELINE_ROOT)
    before = _snapshot(PIPELINE_ROOT)
    candidates = [
        _candidate("test_first", test_file="generated/test_first.py"),
        _candidate("test_nested", test_file="generated/nested/test_nested.py"),
    ]

    materialized = materialize_tests(
        profile,
        candidates,
        tmp_path / "work",
        artifacts_root=tmp_path / "artifacts",
    )

    assert _snapshot(PIPELINE_ROOT) == before
    assert not (PIPELINE_ROOT / "generated").exists()
    assert [test.test_id for test in materialized.tests] == sorted(
        candidate.test_id for candidate in candidates
    )
    written = Path(materialized.project_root) / "generated" / "test_first.py"
    first = next(item for item in candidates if item.test_name == "test_first")
    assert written.read_text(encoding="utf-8") == first.source
    assert all(test.source_digest for test in materialized.tests)
    assert Path(materialized.project_root, "app.py").exists()
    assert Path(materialized.artifacts_root).exists()


def test_materialize_rejects_traversal_collisions_and_duplicate_files(tmp_path):
    profile = make_root_profile(PIPELINE_ROOT)

    with pytest.raises(PipelineError):
        materialize_tests(
            profile,
            [_candidate("test_escape", test_file="../escape/test_escape.py")],
            tmp_path / "escape-work",
        )

    with pytest.raises(PipelineError):
        materialize_tests(
            profile,
            [
                _candidate("test_one", test_file="generated/test_same.py"),
                _candidate("test_two", test_file="generated/test_same.py"),
            ],
            tmp_path / "duplicate-work",
        )

    with pytest.raises(PipelineError):
        materialize_tests(profile, [], PIPELINE_ROOT / "inside-project")


def _response(*proposals: tuple[str, str, str]) -> str:
    return json.dumps(
        {
            "tests": [
                {
                    "test_file": test_file,
                    "test_name": test_name,
                    "source": source,
                }
                for test_file, test_name, source in proposals
            ]
        }
    )


STRONG_SOURCE = """\
from app import classify


def test_classify_strong():
    assert classify(1) == 1
    assert classify(0) == 0
    assert classify(-1) == -1
"""

WEAK_SOURCE = """\
from app import classify


def test_classify_weak():
    classify(1)
    classify(0)
    classify(-1)
"""

FAILING_SOURCE = """\
from app import classify


def test_classify_failing():
    assert classify(1) == 99
"""

IMPORT_ERROR_SOURCE = """\
from app import classify
from missing_module import thing


def test_classify_import_error():
    assert callable(classify)
"""

FLAKY_SOURCE = """\
from pathlib import Path

from app import classify


def test_classify_flaky():
    marker = Path(__file__).with_name("marker.txt")
    if marker.exists():
        assert classify(1) == 1
    else:
        marker.write_text("first run", encoding="utf-8")
        assert classify(1) == 99
"""


def _prepare(responses: dict[str, str]):
    profile = make_root_profile(PIPELINE_ROOT)
    selection = select_targets(profile)
    generation = generate_tests(
        profile,
        selection,
        DeterministicFakeLLM(responses),
        sandbox_health=sandbox_health(),
    )
    return profile, selection, generation


def test_pipeline_keeps_only_the_strong_test():
    response = _response(
        ("generated/test_classify_strong.py", "test_classify_strong", STRONG_SOURCE),
        ("generated/test_classify_weak.py", "test_classify_weak", WEAK_SOURCE),
        ("generated/test_classify_failing.py", "test_classify_failing", FAILING_SOURCE),
        (
            "generated/test_classify_import_error.py",
            "test_classify_import_error",
            IMPORT_ERROR_SOURCE,
        ),
        ("generated/test_classify_flaky.py", "test_classify_flaky", FLAKY_SOURCE),
    )
    profile, selection, generation = _prepare(
        {generation_request_id(SYMBOL): response}
    )
    before = _snapshot(PIPELINE_ROOT)

    report = run_test_pipeline(
        profile,
        selection,
        generation,
        operators=OPERATORS,
        sandbox_config=SandboxConfig(double_run=True, timeout_seconds=30.0),
    )

    assert _snapshot(PIPELINE_ROOT) == before
    assert [item.test_name for item in report.kept] == ["test_classify_strong"]
    kept = report.kept[0]
    assert kept.target_symbol == SYMBOL
    assert kept.mutation_kill_rate == 1.0
    assert kept.mutations_run == 5
    assert kept.mutations_killed == 5

    reasons = {item.test_name: item.reason for item in report.discarded}
    assert reasons == {
        "test_classify_weak": DiscardReason.ASSERTION_TOO_WEAK,
        "test_classify_failing": DiscardReason.RUNTIME_ERROR_ON_HAPPY,
        "test_classify_import_error": DiscardReason.ENVIRONMENT_MISSING,
        "test_classify_flaky": DiscardReason.FLAKY,
    }
    assert len(report.flaky) == 1
    assert report.flaky[0].test_name == "test_classify_flaky"
    assert report.flaky[0].consistent is False

    trace = report.trace
    assert trace.candidates_in == 5
    assert trace.tests_materialized == 5
    assert trace.tests_executed == 5
    assert trace.sandbox_passed == 2
    assert trace.flaky == 1
    assert trace.mutation_scored == 2
    assert trace.mutations_run == 10
    assert trace.mutations_killed == 5
    assert trace.killed_by_assertion == 5
    assert trace.killed_by_exception == 0
    assert trace.killed_by_timeout == 0
    assert trace.kept == 1
    assert trace.discarded_by_reason == {
        DiscardReason.ENVIRONMENT_MISSING: 1,
        DiscardReason.RUNTIME_ERROR_ON_HAPPY: 1,
        DiscardReason.ASSERTION_TOO_WEAK: 1,
        DiscardReason.FLAKY: 1,
    }
    assert trace.double_run is True
    assert trace.kill_threshold == 0.5
    assert trace.operator_ids == [operator.value for operator in OPERATORS]
    assert trace.seed == 0
    assert trace.warnings == []


def test_default_fake_proposal_is_discarded_as_target_mismatch():
    profile, selection, generation = _prepare({})

    report = run_test_pipeline(
        profile,
        selection,
        generation,
        sandbox_config=SandboxConfig(double_run=True, timeout_seconds=30.0),
    )

    assert generation.candidates[0].test_name == "test_classify_smoke"
    assert report.kept == []
    assert len(report.discarded) == 1
    assert report.discarded[0].reason == DiscardReason.TARGET_MISMATCH
    assert "never entered" in report.discarded[0].detail
    assert report.trace.sandbox_passed == 1
    assert report.trace.mutation_scored == 0
    assert report.flaky == []


def test_pipeline_reports_when_double_run_is_disabled():
    profile, selection, generation = _prepare({})

    report = run_test_pipeline(
        profile,
        selection,
        generation,
        sandbox_config=SandboxConfig(double_run=False, timeout_seconds=30.0),
    )

    assert report.trace.double_run is False
    assert report.trace.warnings == [
        "double_run is disabled; this report is excluded from evaluation"
    ]


def _stable(report: PipelineReport):
    data = report.model_dump(mode="json")
    data["trace"].pop("started_at", None)
    data["trace"].pop("elapsed_ms", None)
    return data


def test_pipeline_is_deterministic():
    profile, selection, generation = _prepare({})

    first = run_test_pipeline(
        profile,
        selection,
        generation,
        sandbox_config=SandboxConfig(double_run=False, timeout_seconds=30.0),
    )
    second = run_test_pipeline(
        profile,
        selection,
        generation,
        sandbox_config=SandboxConfig(double_run=False, timeout_seconds=30.0),
    )

    assert _stable(first) == _stable(second)


def test_pipeline_with_no_candidates_emits_an_empty_report():
    profile = make_root_profile(PIPELINE_ROOT)
    selection = select_targets(profile)
    generation = generate_tests(
        profile,
        selection,
        DeterministicFakeLLM({generation_request_id(SYMBOL): json.dumps({"tests": []})}),
        sandbox_health=sandbox_health(),
    )

    report = run_test_pipeline(profile, selection, generation)

    assert report.kept == []
    assert report.discarded == []
    assert report.flaky == []
    assert report.trace.candidates_in == 0
    assert "no candidate tests to execute" in report.trace.warnings
