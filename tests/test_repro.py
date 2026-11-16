from __future__ import annotations

from pathlib import Path

from audit.models import ReproFailureReason, ReproSkipReason
from audit.repro import classify_repro, run_repro
from audit.rule_scan import scan_rules
from audit.triage import llm_triage
from helpers import line_of, make_root_profile
from llm.fake import DeterministicFakeLLM
from review.cross_check import cross_check, subjects_from_drafts
from review.fake import DeterministicReviewer
from review.models import RetractReason, ReviewVerdict
from sandbox import double_run
from sandbox.runner import (
    FailureKind,
    RunnerError,
    RunnerStage,
    SandboxConfig,
    TestOutcome as Outcome,
    TestRunResult as RunResult,
    TestStatus as Status,
)

REPRO_ROOT = Path(__file__).parent / "fixtures" / "audit_project" / "repro"
REVIEW_ROOT = Path(__file__).parent / "fixtures" / "audit_project" / "review"
DEFECTS = REPRO_ROOT / "defects.py"


def _pipeline(root: Path):
    profile = make_root_profile(root)
    scan = scan_rules(profile)
    triage = llm_triage(profile, scan, DeterministicFakeLLM())
    review = cross_check(
        profile, scan, subjects_from_drafts(triage.drafts), DeterministicReviewer()
    )
    return profile, scan, review


def _snapshot(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if "__pycache__" not in path.parts
    }


def _outcome(
    status: Status, failure_kind: FailureKind = FailureKind.NONE
) -> Outcome:
    return Outcome(
        test_id="test_repro.py::test_repro",
        test_file="test_repro.py",
        test_name="test_repro",
        status=status,
        failure_kind=failure_kind,
    )


def _run(
    index: int, status: Status, failure_kind: FailureKind = FailureKind.NONE
) -> RunResult:
    return RunResult(
        run_index=index,
        started_at="2026-01-01T00:00:00+00:00",
        elapsed_ms=1,
        exit_code=0 if status == Status.PASSED else 1,
        timed_out=False,
        tests=[_outcome(status, failure_kind)],
    )


def _report(*runs: RunResult, errors: tuple[RunnerError, ...] = ()):
    return double_run.merge_runs(
        list(runs),
        SandboxConfig(double_run=True),
        "repro",
        runner_errors=list(errors),
    )


def test_r001_template_reproduces_supported_mutable_defaults(tmp_path):
    profile, scan, review = _pipeline(REPRO_ROOT)
    before = _snapshot(REPRO_ROOT)

    report = run_repro(
        profile,
        scan,
        review,
        sandbox_config=SandboxConfig(double_run=False),
        artifact_root=tmp_path,
    )

    assert _snapshot(REPRO_ROOT) == before
    assert report.trace.findings_considered == 8
    assert report.trace.attempted == 6
    assert report.trace.reproduced == 5
    assert report.trace.skipped_by_reason == {
        ReproSkipReason.UNSUPPORTED_SIGNATURE: 1,
        ReproSkipReason.TEMPLATE_BUILD_FAILED: 1,
    }
    assert report.trace.failures_by_reason == {
        ReproFailureReason.CLAIM_NOT_OBSERVED: 1
    }

    artifacts = {artifact.line_start: artifact for artifact in report.artifacts}

    list_artifact = artifacts[line_of(DEFECTS, "def list_default")]
    assert list_artifact.reproduced is True
    assert list_artifact.failure_reason is None
    assert "from defects import list_default" in list_artifact.script_source
    assert '.append("testlens_probe")' in list_artifact.script_source
    assert '"testlens_probe" in second' in list_artifact.script_source
    assert list_artifact.double_run is False
    assert list_artifact.platform
    assert Path(list_artifact.script_path).exists()

    dict_artifact = artifacts[line_of(DEFECTS, "def dict_default")]
    assert 'first["testlens_probe"] = True' in dict_artifact.script_source
    assert dict_artifact.reproduced is True

    set_artifact = artifacts[line_of(DEFECTS, "def set_default")]
    assert '.add("testlens_probe")' in set_artifact.script_source
    assert set_artifact.reproduced is True

    bytearray_artifact = artifacts[line_of(DEFECTS, "def bytearray_default")]
    assert 'extend(b"testlens_probe")' in bytearray_artifact.script_source
    assert 'b"testlens_probe" in second' in bytearray_artifact.script_source
    assert bytearray_artifact.reproduced is True

    tuple_artifact = artifacts[line_of(DEFECTS, "def tuple_default")]
    assert "first[0].append" in tuple_artifact.script_source
    assert "in second[0]" in tuple_artifact.script_source
    assert tuple_artifact.reproduced is True

    copied_artifact = artifacts[line_of(DEFECTS, "def copied_default")]
    assert copied_artifact.reproduced is False
    assert copied_artifact.failure_reason == ReproFailureReason.CLAIM_NOT_OBSERVED
    assert copied_artifact.exit_code != 0

    skipped = {item.line_start: item for item in report.skipped}
    required = skipped[line_of(DEFECTS, "def required_arg")]
    assert required.reason == ReproSkipReason.UNSUPPORTED_SIGNATURE
    assert "item" in (required.detail or "")

    nested = skipped[line_of(DEFECTS, "def nested")]
    assert nested.reason == ReproSkipReason.TEMPLATE_BUILD_FAILED
    assert "module-level function" in (nested.detail or "")


def test_repro_skips_claims_that_did_not_pass_cross_check(tmp_path):
    profile, scan, review = _pipeline(REPRO_ROOT)
    list_line = line_of(DEFECTS, "def list_default")
    target_id = next(
        candidate.finding_id
        for candidate in scan.candidates
        if candidate.line_start == list_line
    )

    results = [
        result.model_copy(
            update={
                "verdict": ReviewVerdict.REFUTE,
                "retract_reason": RetractReason.REFUTED_BY_CONTEXT,
            }
        )
        if result.finding_id == target_id
        else result
        for result in review.results
    ]
    review = review.model_copy(update={"results": results})

    report = run_repro(
        profile,
        scan,
        review,
        sandbox_config=SandboxConfig(double_run=False),
        artifact_root=tmp_path,
    )

    assert report.trace.attempted == 5
    assert report.trace.reproduced == 4
    assert report.trace.skipped_by_reason == {
        ReproSkipReason.NOT_PASSED: 1,
        ReproSkipReason.UNSUPPORTED_SIGNATURE: 1,
        ReproSkipReason.TEMPLATE_BUILD_FAILED: 1,
    }
    assert all(artifact.finding_id != target_id for artifact in report.artifacts)


def test_repro_skips_rules_without_a_template(tmp_path):
    profile, scan, review = _pipeline(REVIEW_ROOT)

    report = run_repro(
        profile,
        scan,
        review,
        sandbox_config=SandboxConfig(double_run=False),
        artifact_root=tmp_path,
    )

    assert report.trace.findings_considered == 5
    assert report.trace.attempted == 1
    assert report.trace.reproduced == 1
    assert report.trace.skipped_by_reason == {
        ReproSkipReason.NOT_PASSED: 2,
        ReproSkipReason.UNSUPPORTED_RULE: 2,
    }


def test_classify_repro_maps_execution_outcomes():
    assert classify_repro(
        _report(_run(0, Status.PASSED), _run(1, Status.PASSED))
    ) == (True, None)

    assert classify_repro(
        _report(
            _run(0, Status.FAILED, FailureKind.ASSERTION),
            _run(1, Status.FAILED, FailureKind.ASSERTION),
        )
    ) == (False, ReproFailureReason.CLAIM_NOT_OBSERVED)

    assert classify_repro(
        _report(
            _run(0, Status.FAILED, FailureKind.IMPORT_ERROR),
            _run(1, Status.FAILED, FailureKind.IMPORT_ERROR),
        )
    ) == (False, ReproFailureReason.SANDBOX_ERROR)

    assert classify_repro(
        _report(
            _run(0, Status.TIMEOUT, FailureKind.TIMEOUT),
            _run(1, Status.TIMEOUT, FailureKind.TIMEOUT),
        )
    ) == (False, ReproFailureReason.TIMEOUT)

    assert classify_repro(
        _report(
            _run(0, Status.PASSED),
            _run(1, Status.FAILED, FailureKind.ASSERTION),
        )
    ) == (False, ReproFailureReason.FLAKY)

    assert classify_repro(
        _report(
            _run(0, Status.PASSED),
            _run(1, Status.PASSED),
            errors=(
                RunnerError(
                    stage=RunnerStage.EXECUTION,
                    message="boom",
                    fatal=True,
                ),
            ),
        )
    ) == (False, ReproFailureReason.SANDBOX_ERROR)


def test_repro_scripts_are_deterministic(tmp_path):
    profile, scan, review = _pipeline(REVIEW_ROOT)

    first = run_repro(
        profile,
        scan,
        review,
        sandbox_config=SandboxConfig(double_run=False),
        artifact_root=tmp_path / "first",
    )
    second = run_repro(
        profile,
        scan,
        review,
        sandbox_config=SandboxConfig(double_run=False),
        artifact_root=tmp_path / "second",
    )

    assert [
        (artifact.finding_id, artifact.script_source)
        for artifact in first.artifacts
    ] == [
        (artifact.finding_id, artifact.script_source)
        for artifact in second.artifacts
    ]
