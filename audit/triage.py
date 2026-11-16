"""LLM triage for deterministic rule candidates.

Triage may only complete a trigger path and restate the claim. Rule identity,
file, and line span come from the deterministic rule scan and are validated
for equality; a response that moves a line, changes a rule, or references an
unknown finding is rejected with an enumerated reason. Untriaged candidates
remain in the rule scan report with ``source=RULE``.
"""

from __future__ import annotations

import ast
import json
import textwrap
import time
import tokenize
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from llm.base import LLMClient
from llm.models import LLMPurpose, LLMRequest, LLMResponse
from sandbox.runner import ProjectProfile

from .models import (
    FindingSource,
    RejectedTriage,
    RuleCandidate,
    RuleScanReport,
    TriageDraft,
    TriageRejectReason,
    TriageReport,
    TriageTrace,
)

TRIAGE_TEMPLATE_VERSION = "triage-v1"
TRIAGE_SYSTEM_PROMPT = (
    "You complete the trigger path for an existing rule finding. The file, "
    "line span, and rule id are fixed; echo them unchanged. Return one JSON "
    'object: {"finding_id": str, "rule_id": str, "line_start": int, '
    '"line_end": int, "trigger_path": [str], "claim": str}. Never invent new '
    "lines, rules, or findings."
)
CONTEXT_LINES = 2


def triage_request_id(finding_id: str) -> str:
    return f"{LLMPurpose.RULE_TRIAGE.value}::{finding_id}"


def _read_source(path: Path) -> tuple[str | None, str | None]:
    try:
        with tokenize.open(path) as handle:
            return handle.read(), None
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _enclosing_function(
    source: str, candidate: RuleCandidate
) -> dict[str, object] | None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    lines = source.splitlines()

    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        if not (node.lineno <= candidate.line_start and candidate.line_end <= end):
            continue
        if best is None or (end - node.lineno) < (
            (best.end_lineno or best.lineno) - best.lineno
        ):
            best = node
    if best is None:
        return None

    end = best.end_lineno or best.lineno
    segment = ast.get_source_segment(source, best) or "\n".join(
        lines[best.lineno - 1 : end]
    )
    return {
        "name": best.name,
        "line_start": best.lineno,
        "line_end": end,
        "source": textwrap.dedent(segment),
    }


def build_triage_prompt(candidate: RuleCandidate, source_text: str) -> str:
    lines = source_text.splitlines()
    before_start = max(candidate.line_start - 1 - CONTEXT_LINES, 0)
    context = {
        "finding_id": candidate.finding_id,
        "rule_id": candidate.rule_id.value,
        "file": candidate.relative_file,
        "line_start": candidate.line_start,
        "line_end": candidate.line_end,
        "column": candidate.column,
        "title": candidate.title,
        "message": candidate.message,
        "snippet": candidate.snippet,
        "snippet_hash": candidate.snippet_hash,
        "source": candidate.source.value,
        "enclosing_function": _enclosing_function(source_text, candidate),
        "context_before": lines[before_start : candidate.line_start - 1],
        "context_after": lines[candidate.line_end : candidate.line_end + CONTEXT_LINES],
    }
    return (
        "Complete the trigger path for the candidate below. Return JSON only.\n"
        "<CANDIDATE_JSON>\n"
        f"{json.dumps(context, sort_keys=True, indent=2)}\n"
        "</CANDIDATE_JSON>\n"
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate(
    text: str,
    candidate: RuleCandidate,
    request: LLMRequest,
    response: LLMResponse,
) -> TriageDraft | RejectedTriage:
    def reject(reason: TriageRejectReason, detail: str) -> RejectedTriage:
        return RejectedTriage(
            finding_id=candidate.finding_id,
            rule_id=candidate.rule_id,
            file=candidate.relative_file,
            reason=reason,
            detail=detail,
            prompt_digest=request.prompt_digest,
            response_digest=response.response_digest,
            model_kind=response.model_kind,
            model_id=response.model_id,
        )

    if not text.strip():
        return reject(
            TriageRejectReason.EMPTY_RESPONSE, "model returned an empty response"
        )

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return reject(TriageRejectReason.INVALID_JSON, f"JSONDecodeError: {exc.msg}")

    if not isinstance(payload, dict):
        return reject(TriageRejectReason.INVALID_SHAPE, "response must be an object")

    finding_id = payload.get("finding_id")
    rule_id = payload.get("rule_id")
    line_start = payload.get("line_start")
    line_end = payload.get("line_end")
    trigger_path = payload.get("trigger_path")
    claim = payload.get("claim")

    if not isinstance(finding_id, str):
        return reject(
            TriageRejectReason.INVALID_SHAPE, "finding_id must be a string"
        )
    if not isinstance(rule_id, str):
        return reject(TriageRejectReason.INVALID_SHAPE, "rule_id must be a string")
    if not _is_int(line_start) or not _is_int(line_end):
        return reject(
            TriageRejectReason.INVALID_SHAPE, "line_start and line_end must be ints"
        )
    if not isinstance(trigger_path, list) or not all(
        isinstance(step, str) for step in trigger_path
    ):
        return reject(
            TriageRejectReason.INVALID_SHAPE,
            "trigger_path must be a list of strings",
        )
    if not isinstance(claim, str):
        return reject(TriageRejectReason.INVALID_SHAPE, "claim must be a string")

    if finding_id != candidate.finding_id:
        return reject(
            TriageRejectReason.UNKNOWN_FINDING,
            f"expected finding {candidate.finding_id}, got {finding_id}",
        )
    if rule_id != candidate.rule_id.value:
        return reject(
            TriageRejectReason.RULE_MISMATCH,
            f"expected rule {candidate.rule_id.value}, got {rule_id}",
        )
    if (line_start, line_end) != (candidate.line_start, candidate.line_end):
        return reject(
            TriageRejectReason.LINE_ANCHOR_MISMATCH,
            f"expected lines {candidate.line_start}-{candidate.line_end}, "
            f"got {line_start}-{line_end}",
        )

    if not claim.strip():
        return reject(TriageRejectReason.EMPTY_CLAIM, "claim must not be empty")

    steps = [step.strip() for step in trigger_path if step.strip()]
    if not steps:
        return reject(
            TriageRejectReason.NO_TRIGGER_PATH, "trigger_path must not be empty"
        )

    return TriageDraft(
        finding_id=candidate.finding_id,
        rule_id=candidate.rule_id,
        source=FindingSource.RULE_WITH_LLM_TRIAGE,
        file=candidate.relative_file,
        line_start=candidate.line_start,
        line_end=candidate.line_end,
        claim=claim.strip(),
        trigger_path=steps,
        prompt_digest=request.prompt_digest,
        response_digest=response.response_digest,
        prompt_template_version=request.prompt_template_version,
        model_kind=response.model_kind,
        model_id=response.model_id,
    )


def llm_triage(
    profile: ProjectProfile,
    report: RuleScanReport,
    llm: LLMClient,
) -> TriageReport:
    """Complete trigger paths for rule candidates; never move a line."""
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    drafts: list[TriageDraft] = []
    rejected: list[RejectedTriage] = []
    counts: Counter[TriageRejectReason] = Counter()
    warnings: set[str] = set()
    sources: dict[str, tuple[str | None, str | None]] = {}

    for candidate in report.candidates:
        relative_file = candidate.relative_file
        if relative_file not in sources:
            path = Path(candidate.file)
            if not path.is_absolute():
                path = Path(profile.root).expanduser().resolve() / path
            sources[relative_file] = _read_source(path)
        source_text, read_error = sources[relative_file]
        if source_text is None:
            rejected.append(
                RejectedTriage(
                    finding_id=candidate.finding_id,
                    rule_id=candidate.rule_id,
                    file=relative_file,
                    reason=TriageRejectReason.SOURCE_UNREADABLE,
                    detail=read_error or "candidate source could not be read",
                    model_kind=llm.model_kind,
                    model_id=llm.model_id,
                )
            )
            counts[TriageRejectReason.SOURCE_UNREADABLE] += 1
            continue

        request = LLMRequest(
            request_id=triage_request_id(candidate.finding_id),
            purpose=LLMPurpose.RULE_TRIAGE,
            prompt_template_version=TRIAGE_TEMPLATE_VERSION,
            system=TRIAGE_SYSTEM_PROMPT,
            prompt=build_triage_prompt(candidate, source_text),
            seed=0,
            temperature=0.0,
        )
        response = llm.complete(request)

        if (
            response.model_kind != llm.model_kind
            or response.model_id != llm.model_id
        ):
            warnings.add(
                f"model identity mismatch for {request.request_id}: "
                f"client {llm.model_kind.value}/{llm.model_id}, "
                f"response {response.model_kind.value}/{response.model_id}"
            )

        outcome = _validate(response.text, candidate, request, response)
        if isinstance(outcome, TriageDraft):
            drafts.append(outcome)
        else:
            rejected.append(outcome)
            counts[outcome.reason] += 1

    if not report.candidates:
        warnings.add("no rule candidates to triage")

    drafts.sort(
        key=lambda draft: (
            draft.file,
            draft.line_start,
            draft.line_end,
            draft.rule_id.value,
            draft.finding_id,
        )
    )
    rejected.sort(
        key=lambda item: (
            item.file,
            item.rule_id.value,
            item.finding_id,
            item.reason.value,
        )
    )

    trace = TriageTrace(
        started_at=started_at,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        llm_model_kind=llm.model_kind,
        llm_model_id=llm.model_id,
        prompt_template_version=TRIAGE_TEMPLATE_VERSION,
        candidates_considered=len(report.candidates),
        drafts_accepted=len(drafts),
        drafts_rejected=len(rejected),
        rejected_by_reason=dict(
            sorted(counts.items(), key=lambda item: item[0].value)
        ),
        warnings=sorted(warnings),
    )
    return TriageReport(drafts=drafts, rejected=rejected, trace=trace)
