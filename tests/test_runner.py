from __future__ import annotations

import platform

from helpers import make_profile, sandbox_config
from sandbox import double_run
from sandbox.runner import (
    FailureKind,
    FinalStatus,
    SandboxConfig,
    TestStatus as Status,
    _ResourceMonitor,
    _build_outcomes,
)


def test_runner_strong_passes_and_covers_target():
    report = double_run.execute(make_profile("test_strong.py"), sandbox_config())

    assert len(report.runs) == 1
    assert len(report.tests) == 1
    result = report.tests[0]

    assert result.final_status == FinalStatus.PASSED
    assert result.target_symbol == "classify"
    assert result.covered_target_symbols_union == ["classify"]
    assert result.runs[0].status == Status.PASSED
    assert result.runs[0].covered_target_symbols[0].entry_hit is True
    assert result.runs[0].covered_target_symbols[0].executed_lines

    assert report.flaky == []
    assert report.timeout_summary.any_timeout is False
    assert report.trace.double_run is False
    assert report.sandbox_config.double_run is False
    assert report.resource_summary.platform == platform.system()
    assert report.runner_errors == []


def test_runner_weak_passes_and_covers_target():
    report = double_run.execute(make_profile("test_weak.py"), sandbox_config())
    result = report.tests[0]

    assert result.final_status == FinalStatus.PASSED
    assert result.target_symbol == "classify"
    assert result.covered_target_symbols_union == ["classify"]
    assert result.runs[0].covered_target_symbols[0].entry_hit is True


def test_pending_current_test_becomes_timeout():
    config = SandboxConfig(timeout_seconds=1.0, double_run=False)
    monitor = _ResourceMonitor(pid=0, config=config)
    outcomes = _build_outcomes(
        {
            "tests": {
                "test_slow.py::test_slow": {
                    "test_id": "test_slow.py::test_slow",
                    "test_file": "test_slow.py",
                    "test_name": "test_slow",
                    "status": "pending",
                    "failure_kind": "none",
                    "covered_symbols": [],
                }
            },
            "current_test": "test_slow.py::test_slow",
            "collection_errors": [],
        },
        config,
        timed_out=True,
        monitor=monitor,
    )

    assert len(outcomes) == 1
    assert outcomes[0].status == Status.TIMEOUT
    assert outcomes[0].failure_kind == FailureKind.TIMEOUT
    assert outcomes[0].timeout.timed_out is True
    assert outcomes[0].timeout.process_tree_killed is True
