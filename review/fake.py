"""Independent deterministic reviewer used by default and in evaluation.

This is deliberately not built on ``llm.fake``: it has its own interface,
implementation, and state. It answers structured review requests directly
instead of parsing prompts, and returns a structured evidence chain.
"""

from __future__ import annotations

from collections.abc import Mapping

from audit.models import RetractReason

from .models import (
    EvidenceKind,
    EvidenceLine,
    EvidenceRole,
    ReviewRequest,
    ReviewerKind,
    ReviewerVerdict,
    ReviewVerdict,
)

REVIEWER_ID = "reviewer:deterministic-v1"


class DeterministicReviewer:
    reviewer_kind = ReviewerKind.FAKE

    def __init__(
        self,
        verdicts: Mapping[str, ReviewerVerdict] | None = None,
        *,
        reviewer_id: str = REVIEWER_ID,
    ) -> None:
        self.reviewer_id = reviewer_id
        self._verdicts = {
            finding_id: verdict.model_copy(deep=True)
            for finding_id, verdict in (verdicts or {}).items()
        }
        self.requests: list[ReviewRequest] = []

    def review(self, request: ReviewRequest) -> ReviewerVerdict:
        self.requests.append(request)

        registered = self._verdicts.get(request.finding_id)
        if registered is not None:
            return registered.model_copy(deep=True)

        if request.rule_id is None:
            # Semantic claims require an explicit verdict; never auto-pass.
            return ReviewerVerdict(
                verdict=ReviewVerdict.UNVERIFIABLE,
                retract_reason=RetractReason.UNVERIFIABLE_CLAIM,
            )

        support = EvidenceLine(
            file=request.file,
            line_start=request.line_start,
            line_end=request.line_end,
            evidence_kind=EvidenceKind.DATA_FLOW,
            role=EvidenceRole.SUPPORTS,
            explanation=(
                "deterministic reviewer accepted the claim for its anchored span"
            ),
            snippet_hash=request.subject_snippet_hash,
            snippet=request.subject_snippet,
        )
        return ReviewerVerdict(
            verdict=ReviewVerdict.PASS,
            evidence_lines=[support],
        )
