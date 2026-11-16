"""Enumerated discard attribution.

Every generated candidate leaves this stage either as a ``KeptTest`` or as a
``DiscardedTest`` carrying exactly one ``DiscardReason``. Nothing is retried
and nothing is silently dropped. Flaky candidates are reported twice: in
``discarded`` and in the report's ``flaky`` list.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, Field
from sandbox.runner import (
    FailureDetail,
    FailureKind,
    FinalStatus,
    FlakyRecord,
    TestResult,
)

from .materialize import PipelineError
from .models import (
    CandidateEvidence,
    DiscardReason,
    DiscardedTest,
    KeptTest,
)

ENVIRONMENT_FAILURES = frozenset(
    {
        FailureKind.IMPORT_ERROR,
        FailureKind.COLLECTION_ERROR,
        FailureKind.RUNNER_ERROR,
    }
)


class AttributionResult(BaseModel):
    kept: list[KeptTest] = Field(default_factory=list)
    discarded: list[DiscardedTest] = Field(default_factory=list)
    flaky: list[FlakyRecord] = Field(default_factory=list)
    discarded_by_reason: dict[DiscardReason, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def covers_target(test_result: TestResult, symbol_id: str) -> bool:
    for outcome in test_result.runs:
        for symbol in outcome.covered_target_symbols:
            if symbol.symbol_id != symbol_id:
                continue
            if symbol.entry_hit or symbol.executed_lines:
                return True
    return False


def _failure_kinds(test_result: TestResult) -> list[FailureKind]:
    kinds = {
        kind for kind in test_result.failure_kinds if kind != FailureKind.NONE
    }
    return sorted(kinds, key=lambda kind: kind.value)


def _flaky_record(test_result: TestResult) -> FlakyRecord:
    return FlakyRecord(
        test_id=test_result.test_id,
        test_file=test_result.test_file,
        test_name=test_result.test_name,
        run_statuses=[outcome.status for outcome in test_result.runs],
        consistent=False,
        failure_details_by_run=[
            FailureDetail(
                run_index=index,
                failure_kind=outcome.failure_kind,
                exception_type=outcome.exception_type,
                message_digest=outcome.message_digest,
            )
            for index, outcome in enumerate(test_result.runs)
        ],
    )


def _discarded(
    evidence: CandidateEvidence,
    reason: DiscardReason,
    detail: str,
) -> DiscardedTest:
    candidate = evidence.candidate
    test_result = evidence.test_result
    mutation = evidence.mutation
    return DiscardedTest(
        test_id=candidate.test_id,
        target_symbol=candidate.target_symbol,
        test_file=candidate.test_file,
        test_name=candidate.test_name,
        reason=reason,
        detail=detail,
        final_status=test_result.final_status if test_result is not None else None,
        failure_kinds=_failure_kinds(test_result) if test_result is not None else [],
        mutation_kill_rate=mutation.kill_rate if mutation is not None else None,
        mutations_run=mutation.mutations_run if mutation is not None else None,
        mutations_killed=mutation.mutations_killed if mutation is not None else None,
    )


def attribute_candidate(
    evidence: CandidateEvidence,
    kill_threshold: float,
) -> tuple[KeptTest | DiscardedTest, FlakyRecord | None]:
    candidate = evidence.candidate
    test_result = evidence.test_result

    if test_result is None:
        errors = "; ".join(error.message for error in evidence.runner_errors)
        detail = "no test result was produced"
        if errors:
            detail += f": {errors}"
        return (
            _discarded(evidence, DiscardReason.ENVIRONMENT_MISSING, detail),
            None,
        )

    if test_result.flaky or test_result.final_status == FinalStatus.FLAKY:
        record = evidence.flaky_record or _flaky_record(test_result)
        statuses = "/".join(status.value for status in record.run_statuses)
        return (
            _discarded(evidence, DiscardReason.FLAKY, f"run statuses differ: {statuses}"),
            record,
        )

    if test_result.final_status == FinalStatus.PASSED:
        if not covers_target(test_result, candidate.target_symbol):
            return (
                _discarded(
                    evidence,
                    DiscardReason.TARGET_MISMATCH,
                    f"test never entered {candidate.target_symbol}",
                ),
                None,
            )
        if evidence.mutation is None:
            raise PipelineError(
                f"candidate {candidate.test_id} passed the sandbox but was not "
                "mutation-scored"
            )
        mutation = evidence.mutation
        if mutation.mutations_run == 0 or mutation.kill_rate < kill_threshold:
            return (
                _discarded(
                    evidence,
                    DiscardReason.ASSERTION_TOO_WEAK,
                    (
                        f"kill rate {mutation.kill_rate:.2f} is below threshold "
                        f"{kill_threshold:.2f} "
                        f"({mutation.mutations_killed}/{mutation.mutations_run})"
                    ),
                ),
                None,
            )
        return (
            KeptTest(
                target_symbol=candidate.target_symbol,
                test_file=candidate.test_file,
                test_name=candidate.test_name,
                mutation_kill_rate=mutation.kill_rate,
                mutations_run=mutation.mutations_run,
                mutations_killed=mutation.mutations_killed,
            ),
            None,
        )

    kinds = set(_failure_kinds(test_result))
    if kinds & ENVIRONMENT_FAILURES or test_result.final_status == FinalStatus.SKIPPED:
        detail = "environment or collection failure"
        if kinds:
            detail += ": " + ", ".join(kind.value for kind in sorted(kinds, key=lambda k: k.value))
        return (
            _discarded(evidence, DiscardReason.ENVIRONMENT_MISSING, detail),
            None,
        )

    detail = f"test did not pass on the happy path ({test_result.final_status.value})"
    if kinds:
        detail += ": " + ", ".join(kind.value for kind in sorted(kinds, key=lambda k: k.value))
    return (
        _discarded(evidence, DiscardReason.RUNTIME_ERROR_ON_HAPPY, detail),
        None,
    )


def attribute_discard(
    evidence: Sequence[CandidateEvidence],
    *,
    kill_threshold: float = 0.5,
) -> AttributionResult:
    if not 0.0 <= kill_threshold <= 1.0:
        raise ValueError("kill_threshold must be between 0 and 1")

    kept: list[KeptTest] = []
    discarded: list[DiscardedTest] = []
    flaky: list[FlakyRecord] = []
    counts: Counter[DiscardReason] = Counter()
    warnings: set[str] = set()
    seen: set[str] = set()

    for item in sorted(evidence, key=lambda entry: entry.candidate.test_id):
        test_id = item.candidate.test_id
        if test_id in seen:
            raise PipelineError(f"duplicate evidence for candidate {test_id}")
        seen.add(test_id)

        outcome, record = attribute_candidate(item, kill_threshold)
        if isinstance(outcome, KeptTest):
            kept.append(outcome)
        else:
            discarded.append(outcome)
            counts[outcome.reason] += 1
        if record is not None:
            flaky.append(record)
            if item.flaky_record is None:
                warnings.add(f"flaky record synthesized for {test_id}")

    kept.sort(
        key=lambda item: (
            item.target_symbol,
            item.test_file,
            item.test_name,
        )
    )
    discarded.sort(
        key=lambda item: (
            item.reason.value,
            item.test_file,
            item.test_name,
            item.test_id,
        )
    )
    flaky.sort(key=lambda item: item.test_id)

    return AttributionResult(
        kept=kept,
        discarded=discarded,
        flaky=flaky,
        discarded_by_reason=dict(
            sorted(counts.items(), key=lambda item: item[0].value)
        ),
        warnings=sorted(warnings),
    )
