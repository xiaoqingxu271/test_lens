from __future__ import annotations

from helpers import make_profile, sandbox_config
from mutation.score import score_test


def _score(test_file: str):
    profile = make_profile(test_file)
    target = profile.target_symbols[0]
    report = score_test(
        profile,
        target,
        profile.test_paths[0],
        sandbox_config=sandbox_config(timeout_seconds=20.0),
    )
    return report


def test_strong_test_kills_mutants_and_weak_test_does_not():
    strong = _score("test_strong.py")
    weak = _score("test_weak.py")

    assert strong.mutations_run > 0
    assert strong.mutations_run == weak.mutations_run
    assert strong.mutant_ids == weak.mutant_ids

    assert strong.mutations_killed == strong.mutations_run
    assert strong.kill_rate == 1.0
    assert strong.killed_by_assertion == strong.mutations_run
    assert strong.killed_by_exception == 0
    assert strong.killed_by_timeout == 0

    assert weak.mutations_killed == 0
    assert weak.kill_rate == 0.0
    assert weak.killed_by_assertion == 0
    assert weak.survived == weak.mutations_run

    assert strong.equivalent_mutants_undetected is True
    assert strong.operator_set_version == "v1"
    assert strong.seed == 0
    assert strong.trace.killed_by_assertion == strong.mutations_run
    assert strong.trace.killed_by_exception == 0
    assert strong.trace.killed_by_timeout == 0
    assert weak.trace.killed_by_assertion == 0
    assert all(result.killed_by == strong.test_id for result in strong.mutant_results)
