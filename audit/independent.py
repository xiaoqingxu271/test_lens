"""LLM-independent semantic audit claims over deterministic anchors.

The model proposes a claim kind, a verbatim quote, a claim, and a trigger
path. The stage resolves the quote against the current function source; the
model never supplies a file, line span, rule id, or finding id. Claims that
overlap a deterministic rule candidate are rejected, so rules keep
precedence. Every rejection is enumerated.
"""

from __future__ import annotations

import ast
import hashlib
import json
import textwrap
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from llm.base import LLMClient
from llm.models import (
    LLMPurpose,
    LLMRequest,
    LLMResponse,
    ModelKind,
    collect_stats,
    stats_delta,
)
from project.files import LoadedModule, load_modules
from sandbox.runner import ProjectProfile

from .models import (
    ClaimKind,
    IndependentClaim,
    IndependentRejectReason,
    IndependentReport,
    IndependentTrace,
    RejectedIndependentClaim,
    RuleScanReport,
)

INDEPENDENT_TEMPLATE_VERSION = "independent-v1"
MAX_QUOTE_CHARS = 400
INDEPENDENT_SYSTEM_PROMPT = (
    "You propose semantic audit claims for one Python function. Return one JSON "
    'object: {"claims": [{"claim_kind": str, "quote": str, "claim": str, '
    '"trigger_path": [str]}]}. The quote must be copied verbatim from the '
    "function source and must occur exactly once in it. Do not provide line "
    "numbers, file paths, rule ids, or finding ids; they are computed "
    "deterministically. Do not propose claims already covered by the listed "
    "rule hits. Return an empty claims list when nothing is defensible."
)


def independent_request_id(relative_file: str, name: str) -> str:
    return f"{LLMPurpose.LLM_INDEPENDENT.value}::{relative_file}::{name}"


def claim_title(claim: IndependentClaim) -> str:
    return f"{claim.claim_kind.value}: {claim.symbol_id.rsplit('::', 1)[-1]}"


def _type_hints(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    hints: list[str] = []
    args = [
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    ]
    for argument in args:
        if argument.annotation is not None:
            hints.append(f"{argument.arg}: {ast.unparse(argument.annotation)}")
    if function.args.vararg is not None and function.args.vararg.annotation is not None:
        hints.append(
            f"*{function.args.vararg.arg}: {ast.unparse(function.args.vararg.annotation)}"
        )
    if function.args.kwarg is not None and function.args.kwarg.annotation is not None:
        hints.append(
            f"**{function.args.kwarg.arg}: {ast.unparse(function.args.kwarg.annotation)}"
        )
    if function.returns is not None:
        hints.append(f"return: {ast.unparse(function.returns)}")
    return hints


def _rule_hits(
    rule_report: RuleScanReport, relative_file: str, line_start: int, line_end: int
) -> list[dict[str, object]]:
    hits = [
        {
            "rule_id": candidate.rule_id.value,
            "line_start": candidate.line_start,
            "line_end": candidate.line_end,
            "title": candidate.title,
        }
        for candidate in rule_report.candidates
        if candidate.relative_file == relative_file
        and candidate.line_start <= line_end
        and line_start <= candidate.line_end
    ]
    hits.sort(
        key=lambda hit: (
            int(hit["line_start"]),
            int(hit["line_end"]),
            str(hit["rule_id"]),
        )
    )
    return hits


def build_independent_prompt(
    module: LoadedModule,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    rule_report: RuleScanReport,
) -> str:
    end = function.end_lineno or function.lineno
    segment = ast.get_source_segment(module.source, function) or "\n".join(
        module.source.splitlines()[function.lineno - 1 : end]
    )
    context = {
        "file": module.relative_file,
        "symbol_id": f"{module.relative_file}::{function.name}",
        "name": function.name,
        "line_start": function.lineno,
        "line_end": end,
        "is_async": isinstance(function, ast.AsyncFunctionDef),
        "source": textwrap.dedent(segment),
        "imports": [
            ast.unparse(node)
            for node in module.tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ],
        "decorators": [
            ast.unparse(decorator) for decorator in function.decorator_list
        ],
        "type_hints": _type_hints(function),
        "rule_hits": _rule_hits(
            rule_report, module.relative_file, function.lineno, end
        ),
        "claim_kinds": [kind.value for kind in ClaimKind],
    }
    return (
        "Propose semantic audit claims for the function below. Return JSON only.\n"
        "<FUNCTION_JSON>\n"
        f"{json.dumps(context, sort_keys=True, indent=2)}\n"
        "</FUNCTION_JSON>\n"
    )


def _module_functions(
    module: LoadedModule,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in module.tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _line_at(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _occurrences(
    text: str, quote: str, start: int, end: int
) -> list[int]:
    positions: list[int] = []
    index = text.find(quote, start, end)
    while index != -1:
        positions.append(index)
        index = text.find(quote, index + 1, end)
    return positions


def _claim_id(
    relative_file: str,
    symbol_id: str,
    claim_kind: ClaimKind,
    line_start: int,
    line_end: int,
    snippet_hash: str,
) -> str:
    payload = "\x1f".join(
        (
            relative_file,
            symbol_id,
            claim_kind.value,
            str(line_start),
            str(line_end),
            snippet_hash,
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"indep::{digest}"


def _snippet_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _rejection(
    *,
    module: LoadedModule,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    reason: IndependentRejectReason,
    detail: str,
    request: LLMRequest | None = None,
    response: LLMResponse | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
) -> RejectedIndependentClaim:
    return RejectedIndependentClaim(
        file=module.relative_file,
        symbol_id=f"{module.relative_file}::{function.name}",
        line_start=line_start,
        line_end=line_end,
        reason=reason,
        detail=detail,
        prompt_digest=request.prompt_digest if request is not None else "",
        response_digest=response.response_digest if response is not None else "",
        model_kind=response.model_kind if response is not None else ModelKind.FAKE,
        model_id=response.model_id if response is not None else "",
    )


def _anchored_claim(
    item: object,
    *,
    module: LoadedModule,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    request: LLMRequest,
    response: LLMResponse,
) -> IndependentClaim | RejectedIndependentClaim:
    def reject(
        reason: IndependentRejectReason,
        detail: str,
        *,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> RejectedIndependentClaim:
        return _rejection(
            module=module,
            function=function,
            reason=reason,
            detail=detail,
            request=request,
            response=response,
            line_start=line_start,
            line_end=line_end,
        )

    if not isinstance(item, dict):
        return reject(IndependentRejectReason.INVALID_SHAPE, "each claim must be an object")

    raw_kind = item.get("claim_kind")
    quote = item.get("quote")
    claim_text = item.get("claim")
    trigger_path = item.get("trigger_path")
    if not all(
        isinstance(value, str) for value in (raw_kind, quote, claim_text)
    ) or not isinstance(trigger_path, list) or not all(
        isinstance(step, str) for step in trigger_path
    ):
        return reject(
            IndependentRejectReason.INVALID_SHAPE,
            "claim_kind, quote, and claim must be strings and trigger_path a "
            "list of strings",
        )

    try:
        kind = ClaimKind(raw_kind)
    except ValueError:
        return reject(
            IndependentRejectReason.UNSUPPORTED_CLAIM_KIND,
            f"unsupported claim kind {raw_kind!r}",
        )

    if not claim_text.strip():
        return reject(IndependentRejectReason.EMPTY_CLAIM, "claim must not be empty")
    steps = [step.strip() for step in trigger_path if step.strip()]
    if not steps:
        return reject(
            IndependentRejectReason.INVALID_SHAPE,
            "trigger_path must contain at least one non-empty step",
        )

    normalized_quote = quote.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized_quote.strip():
        return reject(IndependentRejectReason.EMPTY_QUOTE, "quote must not be empty")
    if len(normalized_quote) > MAX_QUOTE_CHARS:
        return reject(
            IndependentRejectReason.QUOTE_TOO_LONG,
            f"quote exceeds {MAX_QUOTE_CHARS} characters",
        )

    lines = _line_offsets(module.source)
    start_offset = lines[function.lineno - 1]
    end_line = function.end_lineno or function.lineno
    end_offset = lines[end_line]
    positions = _occurrences(
        module.source, normalized_quote, start_offset, end_offset
    )
    if not positions:
        return reject(
            IndependentRejectReason.UNLOCATABLE_QUOTE,
            "quote does not occur inside the function source",
        )
    if len(positions) > 1:
        return reject(
            IndependentRejectReason.AMBIGUOUS_QUOTE,
            f"quote occurs {len(positions)} times inside the function source",
        )

    position = positions[0]
    line_start = _line_at(module.source, position)
    line_end = _line_at(module.source, position + len(normalized_quote) - 1)
    if line_start < function.lineno or line_end > end_line:
        return reject(
            IndependentRejectReason.CLAIM_OUTSIDE_FUNCTION,
            f"resolved span {line_start}-{line_end} leaves the function",
            line_start=line_start,
            line_end=line_end,
        )

    symbol_id = f"{module.relative_file}::{function.name}"
    snippet_hash = _snippet_hash(normalized_quote)
    return IndependentClaim(
        finding_id=_claim_id(
            module.relative_file,
            symbol_id,
            kind,
            line_start,
            line_end,
            snippet_hash,
        ),
        claim_kind=kind,
        file=module.relative_file,
        line_start=line_start,
        line_end=line_end,
        symbol_id=symbol_id,
        claim=claim_text.strip(),
        trigger_path=steps,
        quote=normalized_quote,
        snippet_hash=snippet_hash,
        prompt_digest=request.prompt_digest,
        response_digest=response.response_digest,
        prompt_template_version=request.prompt_template_version,
        model_kind=response.model_kind,
        model_id=response.model_id,
    )


def _validate_response(
    text: str,
    *,
    module: LoadedModule,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    request: LLMRequest,
    response: LLMResponse,
) -> tuple[list[IndependentClaim], list[RejectedIndependentClaim], int]:
    def reject(
        reason: IndependentRejectReason, detail: str
    ) -> tuple[list[IndependentClaim], list[RejectedIndependentClaim], int]:
        return (
            [],
            [
                _rejection(
                    module=module,
                    function=function,
                    reason=reason,
                    detail=detail,
                    request=request,
                    response=response,
                )
            ],
            0,
        )

    if not text.strip():
        return reject(
            IndependentRejectReason.EMPTY_RESPONSE, "model returned an empty response"
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return reject(
            IndependentRejectReason.INVALID_JSON, f"JSONDecodeError: {exc.msg}"
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("claims"), list):
        return reject(
            IndependentRejectReason.INVALID_SHAPE,
            'response must be an object with a "claims" list',
        )

    claims: list[IndependentClaim] = []
    rejected: list[RejectedIndependentClaim] = []
    items = payload["claims"]
    for item in items:
        outcome = _anchored_claim(
            item, module=module, function=function, request=request, response=response
        )
        if isinstance(outcome, IndependentClaim):
            claims.append(outcome)
        else:
            rejected.append(outcome)
    return claims, rejected, len(items)


def _overlaps_rule(
    claim: IndependentClaim, rule_report: RuleScanReport
) -> bool:
    return any(
        candidate.relative_file == claim.file
        and candidate.line_start <= claim.line_end
        and claim.line_start <= candidate.line_end
        for candidate in rule_report.candidates
    )


def scan_independent(
    profile: ProjectProfile,
    rule_report: RuleScanReport,
    llm: LLMClient,
) -> IndependentReport:
    """Propose, anchor, and deduplicate semantic claims for module functions."""
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    stats_before = collect_stats(llm)

    root = Path(profile.root).expanduser().resolve()
    modules, skipped_files = load_modules(
        root, excluded_paths=profile.test_paths
    )

    pending: list[IndependentClaim] = []
    rejected: list[RejectedIndependentClaim] = [
        RejectedIndependentClaim(
            file=skipped.file,
            reason=IndependentRejectReason.SOURCE_UNREADABLE,
            detail=(
                f"{skipped.reason.value}: {skipped.detail}"
                if skipped.detail
                else skipped.reason.value
            ),
        )
        for skipped in skipped_files
    ]
    counts: Counter[IndependentRejectReason] = Counter()
    warnings: set[str] = set()
    functions_considered = 0
    requests_sent = 0
    claims_received = 0

    for module in modules:
        for function in _module_functions(module):
            functions_considered += 1
            request = LLMRequest(
                request_id=independent_request_id(
                    module.relative_file, function.name
                ),
                purpose=LLMPurpose.LLM_INDEPENDENT,
                prompt_template_version=INDEPENDENT_TEMPLATE_VERSION,
                system=INDEPENDENT_SYSTEM_PROMPT,
                prompt=build_independent_prompt(module, function, rule_report),
                seed=0,
                temperature=0.0,
            )
            response = llm.complete(request)
            requests_sent += 1

            if (
                response.model_kind != llm.model_kind
                or response.model_id != llm.model_id
            ):
                warnings.add(
                    f"model identity mismatch for {request.request_id}: "
                    f"client {llm.model_kind.value}/{llm.model_id}, "
                    f"response {response.model_kind.value}/{response.model_id}"
                )

            claims, rejected_items, received = _validate_response(
                response.text,
                module=module,
                function=function,
                request=request,
                response=response,
            )
            claims_received += received
            pending.extend(claims)
            rejected.extend(rejected_items)

    if not modules:
        warnings.add("no modules scanned for independent claims")

    pending.sort(
        key=lambda claim: (
            claim.file,
            claim.line_start,
            claim.line_end,
            claim.claim_kind.value,
            claim.finding_id,
        )
    )

    accepted: list[IndependentClaim] = []
    seen: set[str] = set()
    for claim in pending:
        if _overlaps_rule(claim, rule_report):
            rejected.append(
                RejectedIndependentClaim(
                    file=claim.file,
                    symbol_id=claim.symbol_id,
                    line_start=claim.line_start,
                    line_end=claim.line_end,
                    reason=IndependentRejectReason.DUPLICATE_OF_RULE,
                    detail="claim span overlaps a deterministic rule candidate",
                    prompt_digest=claim.prompt_digest,
                    response_digest=claim.response_digest,
                    model_kind=claim.model_kind,
                    model_id=claim.model_id,
                )
            )
            continue
        if claim.finding_id in seen:
            rejected.append(
                RejectedIndependentClaim(
                    file=claim.file,
                    symbol_id=claim.symbol_id,
                    line_start=claim.line_start,
                    line_end=claim.line_end,
                    reason=IndependentRejectReason.DUPLICATE_CLAIM,
                    detail="another claim resolved to the same anchor",
                    prompt_digest=claim.prompt_digest,
                    response_digest=claim.response_digest,
                    model_kind=claim.model_kind,
                    model_id=claim.model_id,
                )
            )
            continue
        seen.add(claim.finding_id)
        accepted.append(claim)

    for item in rejected:
        counts[item.reason] += 1

    accepted.sort(
        key=lambda claim: (
            claim.file,
            claim.line_start,
            claim.line_end,
            claim.claim_kind.value,
            claim.finding_id,
        )
    )
    rejected.sort(
        key=lambda item: (
            item.file,
            item.line_start or 0,
            item.line_end or 0,
            item.symbol_id or "",
            item.reason.value,
            item.detail,
        )
    )

    stats = stats_delta(stats_before, collect_stats(llm))
    trace = IndependentTrace(
        started_at=started_at,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        files_scanned=len(modules),
        functions_considered=functions_considered,
        requests_sent=requests_sent,
        claims_received=claims_received,
        claims_accepted=len(accepted),
        claims_rejected=len(rejected),
        rejected_by_reason=dict(
            sorted(counts.items(), key=lambda item: item[0].value)
        ),
        llm_model_kind=llm.model_kind,
        llm_model_id=llm.model_id,
        prompt_template_version=INDEPENDENT_TEMPLATE_VERSION,
        llm_calls=stats.calls,
        llm_retries=stats.retries,
        prompt_tokens=stats.prompt_tokens,
        completion_tokens=stats.completion_tokens,
        replayed=stats.replayed,
        warnings=sorted(warnings),
    )
    return IndependentReport(claims=accepted, rejected=rejected, trace=trace)
