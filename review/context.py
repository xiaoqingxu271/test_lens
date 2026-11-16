"""Deterministic context assembly for cross-check.

The reviewer sees the claim, its anchors, the enclosing function, file-level
imports, routes, callers, type hints, existing guards, and other rule hits in
the same file. It does not see triage reasoning.
"""

from __future__ import annotations

import ast
import hashlib
import json
import textwrap

from audit.models import RuleScanReport

from .models import (
    CallerRef,
    CrossCheckSubject,
    EnclosingFunction,
    GuardRef,
    OtherRuleHit,
    ReviewContext,
    ReviewRequest,
)
from .source import ReviewSource, snippet_hash


def _enclosing_function(
    source: ReviewSource, subject: CrossCheckSubject
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(source.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        if not (node.lineno <= subject.line_start and subject.line_end <= end):
            continue
        if best is None or (end - node.lineno) < (
            (best.end_lineno or best.lineno) - best.lineno
        ):
            best = node
    return best


def _type_hints(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    hints: list[str] = []
    args = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
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


def _callers(
    source: ReviewSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> list[CallerRef]:
    if function is None:
        return []
    callers: list[CallerRef] = []
    for node in ast.walk(source.tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node is function:
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == function.name:
                callers.append(CallerRef(name=node.name, line=child.lineno))
    callers.sort(key=lambda caller: (caller.line, caller.name))
    return callers


def _guards(
    source: ReviewSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> list[GuardRef]:
    if function is None:
        return []
    lines = source.lines
    guards: dict[int, GuardRef] = {}
    for node in ast.walk(function):
        if not isinstance(node, (ast.If, ast.Assert, ast.While)):
            continue
        line = node.lineno
        if 1 <= line <= len(lines):
            guards[line] = GuardRef(line=line, source=lines[line - 1].strip())
    return [guards[line] for line in sorted(guards)]


def _other_rule_hits(
    subject: CrossCheckSubject, rule_report: RuleScanReport
) -> list[OtherRuleHit]:
    hits = [
        OtherRuleHit(
            finding_id=candidate.finding_id,
            rule_id=candidate.rule_id,
            line_start=candidate.line_start,
            line_end=candidate.line_end,
            title=candidate.title,
            message=candidate.message,
        )
        for candidate in rule_report.candidates
        if candidate.relative_file == subject.file
        and candidate.finding_id != subject.finding_id
    ]
    hits.sort(
        key=lambda hit: (hit.line_start, hit.line_end, hit.rule_id.value, hit.finding_id)
    )
    return hits


def _hashed(context: ReviewContext) -> ReviewContext:
    payload = context.model_dump(exclude={"source_context_hash"})
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return context.model_copy(update={"source_context_hash": digest})


def build_review_context(
    subject: CrossCheckSubject,
    source: ReviewSource | None,
    rule_report: RuleScanReport,
) -> ReviewContext:
    other_hits = _other_rule_hits(subject, rule_report)

    if source is None:
        return _hashed(
            ReviewContext(
                file=subject.file,
                file_line_count=0,
                other_rule_hits=other_hits,
            )
        )

    function = _enclosing_function(source, subject)
    enclosing = None
    if function is not None:
        end = function.end_lineno or function.lineno
        segment = ast.get_source_segment(source.text, function) or "\n".join(
            source.lines[function.lineno - 1 : end]
        )
        enclosing = EnclosingFunction(
            name=function.name,
            line_start=function.lineno,
            line_end=end,
            source=textwrap.dedent(segment),
        )

    imports = [
        ast.unparse(node)
        for node in source.tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    routes = (
        [ast.unparse(decorator) for decorator in function.decorator_list]
        if function is not None
        else []
    )

    return _hashed(
        ReviewContext(
            file=subject.file,
            file_line_count=len(source.lines),
            imports=imports,
            enclosing_function=enclosing,
            routes=routes,
            callers=_callers(source, function),
            type_hints=_type_hints(function) if function is not None else [],
            guards=_guards(source, function),
            other_rule_hits=other_hits,
        )
    )


def build_review_request(
    subject: CrossCheckSubject,
    source: ReviewSource,
    context: ReviewContext,
) -> ReviewRequest:
    snippet = source.snippet(subject.line_start, subject.line_end)
    return ReviewRequest(
        request_id=f"cross_check::{subject.finding_id}",
        finding_id=subject.finding_id,
        rule_id=subject.rule_id,
        file=subject.file,
        line_start=subject.line_start,
        line_end=subject.line_end,
        claim=subject.claim,
        trigger_path=list(subject.trigger_path),
        subject_snippet=snippet,
        subject_snippet_hash=snippet_hash(snippet),
        context=context,
    )
