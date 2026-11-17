"""LLM-backed reviewer for semantic (``rule_id=None``) claims.

The model chooses a verdict and proposes evidence by verbatim quote only. The
stage resolves every quote inside the subject file, computes the line span and
snippet hash, and rejects the whole verdict if any evidence line cannot be
anchored exactly once. Rule-backed claims never reach this reviewer.
"""

from __future__ import annotations

import json
from pathlib import Path

from audit.models import RetractReason
from llm.base import LLMClient
from llm.models import LLMPurpose, LLMRequest
from sandbox.runner import ProjectProfile

from .models import (
    EvidenceKind,
    EvidenceLine,
    EvidenceRole,
    ReviewRejectReason,
    ReviewRequest,
    ReviewerKind,
    ReviewerVerdict,
    ReviewVerdict,
)
from .source import ReviewSource, load_review_source, snippet_hash

REVIEW_TEMPLATE_VERSION = "review-v1"
MAX_QUOTE_CHARS = 400
REVIEWER_ID = "llm-reviewer-v1"
REVIEW_SYSTEM_PROMPT = (
    "You review one semantic audit claim about a Python function. Return one "
    'JSON object: {"verdict": "pass" | "refute" | "unverifiable", '
    '"evidence": [{"quote": str, "kind": str, "role": "supports" | "refutes", '
    '"explanation": str}]}. A pass needs at least one supports line; a refute '
    "needs at least one refutes line. The quote must be copied verbatim from "
    "the file and must occur exactly once in it. Do not provide file paths, "
    "line numbers, snippet hashes, retract reasons, rule ids, or finding ids; "
    "they are computed deterministically. Return unverifiable when the claim "
    "cannot be decided from the given context."
)
_EVIDENCE_KINDS = {kind.value for kind in EvidenceKind}
_EVIDENCE_ROLES = {role.value for role in EvidenceRole}
_VERDICTS = {verdict.value for verdict in ReviewVerdict}


def build_review_prompt(request: ReviewRequest) -> str:
    context = request.model_dump(mode="json")
    return (
        "Review the semantic claim below. Return JSON only.\n"
        "<REVIEW_JSON>\n"
        f"{json.dumps(context, sort_keys=True, indent=2)}\n"
        "</REVIEW_JSON>\n"
    )


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _occurrences(text: str, quote: str) -> list[int]:
    positions: list[int] = []
    index = text.find(quote)
    while index != -1:
        positions.append(index)
        index = text.find(quote, index + 1)
    return positions


def _rejection(reason: ReviewRejectReason) -> ReviewerVerdict:
    return ReviewerVerdict(
        verdict=ReviewVerdict.UNVERIFIABLE,
        retract_reason=RetractReason.UNVERIFIABLE_CLAIM,
        rejection_reason=reason,
    )


def _anchor(
    item: object, source: ReviewSource
) -> EvidenceLine | ReviewRejectReason:
    if not isinstance(item, dict):
        return ReviewRejectReason.INVALID_SHAPE

    quote = item.get("quote")
    raw_kind = item.get("kind")
    raw_role = item.get("role")
    explanation = item.get("explanation")
    if not all(
        isinstance(value, str) for value in (quote, raw_kind, raw_role, explanation)
    ):
        return ReviewRejectReason.INVALID_SHAPE

    if raw_kind not in _EVIDENCE_KINDS:
        return ReviewRejectReason.UNKNOWN_KIND
    if raw_role not in _EVIDENCE_ROLES:
        return ReviewRejectReason.UNKNOWN_ROLE
    if not explanation.strip():
        return ReviewRejectReason.EMPTY_EXPLANATION

    normalized_quote = quote.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized_quote.strip():
        return ReviewRejectReason.EMPTY_QUOTE
    if len(normalized_quote) > MAX_QUOTE_CHARS:
        return ReviewRejectReason.QUOTE_TOO_LONG

    positions = _occurrences(source.text, normalized_quote)
    if not positions:
        return ReviewRejectReason.UNLOCATABLE_QUOTE
    if len(positions) > 1:
        return ReviewRejectReason.AMBIGUOUS_QUOTE

    position = positions[0]
    line_start = _line_at(source.text, position)
    line_end = _line_at(source.text, position + len(normalized_quote) - 1)
    snippet = source.snippet(line_start, line_end)
    return EvidenceLine(
        file=source.relative_file,
        line_start=line_start,
        line_end=line_end,
        evidence_kind=EvidenceKind(raw_kind),
        role=EvidenceRole(raw_role),
        explanation=explanation.strip(),
        snippet_hash=snippet_hash(snippet),
        snippet=snippet,
    )


def _validate_response(text: str, source: ReviewSource) -> ReviewerVerdict:
    if not text.strip():
        return _rejection(ReviewRejectReason.EMPTY_RESPONSE)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _rejection(ReviewRejectReason.INVALID_JSON)
    if not isinstance(payload, dict):
        return _rejection(ReviewRejectReason.INVALID_SHAPE)

    raw_verdict = payload.get("verdict")
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_verdict, str) or not isinstance(raw_evidence, list):
        return _rejection(ReviewRejectReason.INVALID_SHAPE)
    if raw_verdict not in _VERDICTS:
        return _rejection(ReviewRejectReason.UNKNOWN_VERDICT)

    evidence: list[EvidenceLine] = []
    for item in raw_evidence:
        outcome = _anchor(item, source)
        if isinstance(outcome, ReviewRejectReason):
            return _rejection(outcome)
        evidence.append(outcome)

    return ReviewerVerdict(
        verdict=ReviewVerdict(raw_verdict),
        evidence_lines=evidence,
    )


class LLMReviewer:
    """A ``ClaimReviewer`` backed by the provider-neutral LLM client."""

    reviewer_kind = ReviewerKind.MODEL
    reviewer_id = REVIEWER_ID

    def __init__(self, profile: ProjectProfile, llm: LLMClient) -> None:
        self._root = Path(profile.root).expanduser().resolve()
        self._llm = llm
        self._sources: dict[str, ReviewSource | None] = {}
        self.warnings: list[str] = []
        self.requests: list[LLMRequest] = []

    def _source(self, relative_file: str) -> ReviewSource | None:
        if relative_file not in self._sources:
            source, error = load_review_source(self._root, relative_file)
            if source is None:
                self.warnings.append(
                    f"source unavailable for {relative_file}: {error}"
                )
            self._sources[relative_file] = source
        return self._sources[relative_file]

    def review(self, request: ReviewRequest) -> ReviewerVerdict:
        source = self._source(request.file)
        if source is None:
            return ReviewerVerdict(
                verdict=ReviewVerdict.UNVERIFIABLE,
                retract_reason=RetractReason.UNVERIFIABLE_CLAIM,
            )

        llm_request = LLMRequest(
            request_id=request.request_id,
            purpose=LLMPurpose.CLAIM_REVIEW,
            prompt_template_version=REVIEW_TEMPLATE_VERSION,
            system=REVIEW_SYSTEM_PROMPT,
            prompt=build_review_prompt(request),
            seed=0,
            temperature=0.0,
        )
        self.requests.append(llm_request)
        response = self._llm.complete(llm_request)

        if (
            response.model_kind != self._llm.model_kind
            or response.model_id != self._llm.model_id
        ):
            self.warnings.append(
                f"model identity mismatch for {llm_request.request_id}: "
                f"client {self._llm.model_kind.value}/{self._llm.model_id}, "
                f"response {response.model_kind.value}/{response.model_id}"
            )

        return _validate_response(response.text, source)
