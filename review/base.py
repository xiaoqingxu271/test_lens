"""The independent reviewer interface.

This interface is deliberately separate from ``llm.LLMClient``: cross-check
takes structured review requests, not prompts, and a reviewer returns a
structured verdict with evidence lines.
"""

from __future__ import annotations

from typing import Protocol

from .models import ReviewRequest, ReviewerKind, ReviewerVerdict


class ClaimReviewer(Protocol):
    reviewer_kind: ReviewerKind
    reviewer_id: str

    def review(self, request: ReviewRequest) -> ReviewerVerdict:
        ...
