"""Deterministic rule reviewer.

Rules run first. A candidate is confirmed only if the rule still produces the
same span in the current source, and no enumerated context refutation applies
(v1: R005 ``try/finally: close()`` and ``ExitStack.enter_context``). Free-text
review never overrides a rule verdict.
"""

from __future__ import annotations

import ast

from audit.models import RuleId
from audit.rules import scan_tree
from project.files import LoadedModule

from .models import (
    CrossCheckSubject,
    EvidenceKind,
    EvidenceLine,
    EvidenceRole,
    RetractReason,
    ReviewerKind,
    ReviewerVerdict,
    ReviewVerdict,
)
from .source import ReviewSource, line_is_located, make_evidence


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _overlaps(node: ast.AST, subject: CrossCheckSubject) -> bool:
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", None) or start
    return start <= subject.line_end and end >= subject.line_start


def _assigned_names(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> set[str]:
    parent = parents.get(call)
    names: set[str] = set()
    if not isinstance(parent, (ast.Assign, ast.AnnAssign)):
        return names
    if getattr(parent, "value", None) is not call:
        return names
    targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
    for target in targets:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            names.update(
                element.id
                for element in target.elts
                if isinstance(element, ast.Name)
            )
    return names


def _close_calls(node: ast.AST) -> list[tuple[str, ast.Call]]:
    calls: list[tuple[str, ast.Call]] = []
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "close"
            and isinstance(child.func.value, ast.Name)
        ):
            calls.append((child.func.value.id, child))
    return calls


def _enclosing_scope(
    node: ast.AST, parents: dict[ast.AST, ast.AST], tree: ast.Module
) -> ast.AST:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return current
    return tree


def _callable_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = [func.attr]
        value: ast.AST = func.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    return None


def _try_finally_evidence(
    call: ast.Call,
    source: ReviewSource,
    parents: dict[ast.AST, ast.AST],
) -> EvidenceLine | None:
    assigned = _assigned_names(call, parents)
    if not assigned:
        return None
    scope = _enclosing_scope(call, parents, source.tree)
    for node in ast.walk(scope):
        if not isinstance(node, ast.Try):
            continue
        for statement in node.finalbody:
            for receiver, close_call in _close_calls(statement):
                if receiver not in assigned:
                    continue
                return make_evidence(
                    source,
                    close_call.lineno,
                    close_call.end_lineno or close_call.lineno,
                    kind=EvidenceKind.DATA_FLOW,
                    role=EvidenceRole.REFUTES,
                    explanation=(
                        f"`{receiver}` from line {call.lineno} is closed in a "
                        "finally block; R005 does not model try/finally."
                    ),
                )
    return None


def _exit_stack_evidence(
    call: ast.Call,
    source: ReviewSource,
    parents: dict[ast.AST, ast.AST],
) -> EvidenceLine | None:
    current: ast.AST = call
    enter_context: ast.Call | None = None
    while current in parents:
        parent = parents[current]
        if (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Attribute)
            and parent.func.attr == "enter_context"
            and any(argument is call for argument in parent.args)
        ):
            enter_context = parent
            break
        current = parent
    if enter_context is None:
        return None

    current = enter_context
    while current in parents:
        current = parents[current]
        if not isinstance(current, (ast.With, ast.AsyncWith)):
            continue
        for item in current.items:
            expression = item.context_expr
            if not isinstance(expression, ast.Call):
                continue
            if _callable_name(expression.func) in {"ExitStack", "contextlib.ExitStack"}:
                return make_evidence(
                    source,
                    enter_context.lineno,
                    enter_context.end_lineno or enter_context.lineno,
                    kind=EvidenceKind.GUARD,
                    role=EvidenceRole.REFUTES,
                    explanation=(
                        "resource is managed by ExitStack.enter_context; "
                        "R005 does not model it."
                    ),
                )
    return None


def _context_refutation(
    subject: CrossCheckSubject, source: ReviewSource
) -> EvidenceLine | None:
    if subject.rule_id is not RuleId.R005_RESOURCE_NO_WITH:
        return None

    parents = _parents(source.tree)
    calls = [
        node
        for node in ast.walk(source.tree)
        if isinstance(node, ast.Call) and _overlaps(node, subject)
    ]
    for call in calls:
        evidence = _try_finally_evidence(call, source, parents)
        if evidence is not None:
            return evidence
        evidence = _exit_stack_evidence(call, source, parents)
        if evidence is not None:
            return evidence
    return None


class RuleReviewer:
    reviewer_kind = ReviewerKind.RULE
    reviewer_id = "rule-reviewer-v1"

    def review(
        self,
        subject: CrossCheckSubject,
        source: ReviewSource | None,
    ) -> ReviewerVerdict:
        if subject.rule_id is None:
            raise ValueError("rule reviewer requires a rule-backed subject")
        rule_name = subject.rule_id.value

        if source is None:
            return ReviewerVerdict(
                verdict=ReviewVerdict.UNVERIFIABLE,
                retract_reason=RetractReason.UNVERIFIABLE_CLAIM,
                checked_rule_ids=[rule_name],
            )

        module = LoadedModule(
            file=source.absolute,
            relative_file=source.relative_file,
            source=source.text,
            tree=source.tree,
        )
        candidates = scan_tree(module, (subject.rule_id,))
        matching = next(
            (
                candidate
                for candidate in candidates
                if (candidate.line_start, candidate.line_end)
                == (subject.line_start, subject.line_end)
            ),
            None,
        )

        if matching is None:
            if not line_is_located(source, subject.line_start, subject.line_end):
                return ReviewerVerdict(
                    verdict=ReviewVerdict.UNVERIFIABLE,
                    retract_reason=RetractReason.NO_LINE_LOCATION,
                    checked_rule_ids=[rule_name],
                )
            evidence = make_evidence(
                source,
                subject.line_start,
                subject.line_end,
                kind=EvidenceKind.RULE,
                role=EvidenceRole.REFUTES,
                explanation=(
                    f"{rule_name} no longer matches lines "
                    f"{subject.line_start}-{subject.line_end}; context changed."
                ),
            )
            return ReviewerVerdict(
                verdict=ReviewVerdict.REFUTE,
                evidence_lines=[evidence],
                retract_reason=RetractReason.REFUTED_BY_CONTEXT,
                checked_rule_ids=[rule_name],
            )

        refutation = _context_refutation(subject, source)
        if refutation is not None:
            return ReviewerVerdict(
                verdict=ReviewVerdict.REFUTE,
                evidence_lines=[refutation],
                retract_reason=RetractReason.REFUTED_BY_CONTEXT,
                checked_rule_ids=[rule_name],
            )

        support = EvidenceLine(
            file=subject.file,
            line_start=matching.line_start,
            line_end=matching.line_end,
            evidence_kind=EvidenceKind.RULE,
            role=EvidenceRole.SUPPORTS,
            explanation=f"{rule_name} still matches this span.",
            snippet_hash=matching.snippet_hash,
            snippet=matching.snippet,
        )
        return ReviewerVerdict(
            verdict=ReviewVerdict.PASS,
            evidence_lines=[support],
            checked_rule_ids=[rule_name],
        )
