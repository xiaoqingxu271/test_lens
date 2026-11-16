"""Deterministic v1 audit rules with precise line anchors."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from project.files import LoadedModule

from .models import FindingSource, RuleCandidate, RuleId


MUTABLE_CONSTRUCTORS = frozenset({"list", "dict", "set", "bytearray"})

RULE_TITLES: dict[RuleId, str] = {
    RuleId.R001_MUTABLE_DEFAULT: "Mutable default argument",
    RuleId.R002_NONE_DEREF_NO_GUARD: "None dereference without a guard",
    RuleId.R003_UNREACHABLE_AFTER_RETURN: "Unreachable code after return/raise",
    RuleId.R004_BARE_EXCEPT_SWALLOW: "Bare except swallows exceptions",
    RuleId.R005_RESOURCE_NO_WITH: "Resource opened without with",
}

ALL_RULES: tuple[RuleId, ...] = (
    RuleId.R001_MUTABLE_DEFAULT,
    RuleId.R002_NONE_DEREF_NO_GUARD,
    RuleId.R003_UNREACHABLE_AFTER_RETURN,
    RuleId.R004_BARE_EXCEPT_SWALLOW,
    RuleId.R005_RESOURCE_NO_WITH,
)


@dataclass
class RuleContext:
    file: Path
    relative_file: str
    source: str
    tree: ast.Module

    @property
    def source_lines(self) -> list[str]:
        return self.source.splitlines()

    def snippet(self, line_start: int, line_end: int) -> str:
        lines = self.source_lines
        start = max(1, line_start)
        end = max(start, line_end)
        return "\n".join(lines[start - 1 : end])

    def candidate(
        self,
        rule_id: RuleId,
        *,
        line_start: int,
        line_end: int,
        column: int,
        message: str,
    ) -> RuleCandidate:
        snippet = self.snippet(line_start, line_end)
        snippet_hash = hashlib.sha256(snippet.encode("utf-8")).hexdigest()[:16]
        finding_id = hashlib.sha256(
            (
                f"{rule_id.value}|{self.relative_file}|{line_start}|{line_end}|"
                f"{column}|{snippet_hash}"
            ).encode("utf-8")
        ).hexdigest()[:16]
        return RuleCandidate(
            finding_id=finding_id,
            rule_id=rule_id,
            file=str(self.file),
            relative_file=self.relative_file,
            line_start=line_start,
            line_end=line_end,
            column=column,
            title=RULE_TITLES[rule_id],
            message=message,
            snippet=snippet,
            snippet_hash=snippet_hash,
            source=FindingSource.RULE,
        )


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in target.elts:
            names.update(_target_names(element))
        return names
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return set()


def _statement_lists(statement: ast.stmt) -> list[list[ast.stmt]]:
    lists: list[list[ast.stmt]] = []
    if isinstance(statement, ast.If):
        lists.extend([statement.body, statement.orelse])
    elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
        lists.extend([statement.body, statement.orelse])
    elif isinstance(statement, (ast.With, ast.AsyncWith)):
        lists.append(statement.body)
    elif isinstance(statement, ast.Try):
        lists.append(statement.body)
        lists.extend(handler.body for handler in statement.handlers)
        lists.extend([statement.orelse, statement.finalbody])
    elif isinstance(statement, ast.Match):
        lists.extend(case.body for case in statement.cases)
    elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        lists.append(statement.body)
    return [items for items in lists if items]


def _function_defaults(args: ast.arguments) -> list[tuple[str, ast.AST]]:
    positional = list(args.posonlyargs) + list(args.args)
    defaults: list[tuple[str, ast.AST]] = []
    if args.defaults:
        paired = positional[-len(args.defaults) :]
        defaults.extend(
            (argument.arg, default)
            for argument, default in zip(paired, args.defaults)
        )
    for argument, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            defaults.append((argument.arg, default))
    return defaults


def _collect_module_bindings(tree: ast.Module) -> set[str]:
    names: set[str] = set()

    def visit(statements: list[ast.stmt]) -> None:
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(statement.name)
                continue
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.name == "*":
                        continue
                    names.add(alias.asname or alias.name.split(".")[0])
                continue
            if isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    if alias.name == "*":
                        continue
                    names.add(alias.asname or alias.name)
                continue
            if isinstance(statement, ast.Assign):
                for target in statement.targets:
                    names.update(_target_names(target))
            elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
                names.update(_target_names(statement.target))
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                names.update(_target_names(statement.target))
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    if item.optional_vars is not None:
                        names.update(_target_names(item.optional_vars))
            elif isinstance(statement, ast.Try):
                for handler in statement.handlers:
                    if handler.name:
                        names.add(handler.name)
            for child_list in _statement_lists(statement):
                visit(child_list)

    visit(tree.body)
    return names


def _is_builtin_constructor_call(
    call: ast.Call,
    module_bindings: set[str],
) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in MUTABLE_CONSTRUCTORS and func.id not in module_bindings
    if isinstance(func, ast.Attribute) and func.attr in MUTABLE_CONSTRUCTORS:
        return ast.unparse(func) == f"builtins.{func.attr}"
    return False


def _is_mutable_default(node: ast.AST, module_bindings: set[str]) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set)):
        return True
    if isinstance(node, (ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Tuple):
        return any(_is_mutable_default(element, module_bindings) for element in node.elts)
    if isinstance(node, ast.Call):
        return _is_builtin_constructor_call(node, module_bindings)
    return False


def check_r001(context: RuleContext) -> list[RuleCandidate]:
    module_bindings = _collect_module_bindings(context.tree)
    candidates: list[RuleCandidate] = []

    for node in ast.walk(context.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            defaults = _function_defaults(node.args)
        else:
            continue

        mutable = [
            (name, default)
            for name, default in defaults
            if _is_mutable_default(default, module_bindings)
        ]
        if not mutable:
            continue

        line_start = min(default.lineno for _, default in mutable)
        line_end = max(default.end_lineno or default.lineno for _, default in mutable)
        column = min(default.col_offset for _, default in mutable)
        names = ", ".join(name for name, _ in mutable)
        candidates.append(
            context.candidate(
                RuleId.R001_MUTABLE_DEFAULT,
                line_start=line_start,
                line_end=line_end,
                column=column,
                message=f"Mutable default argument(s) shared across calls: {names}.",
            )
        )

    return candidates


def check_r003(context: RuleContext) -> list[RuleCandidate]:
    candidates: list[RuleCandidate] = []

    def visit(statements: list[ast.stmt]) -> None:
        for index, statement in enumerate(statements):
            if isinstance(statement, (ast.Return, ast.Raise)):
                unreachable = statements[index + 1 :]
                if unreachable:
                    first = unreachable[0]
                    last = unreachable[-1]
                    terminator = "return" if isinstance(statement, ast.Return) else "raise"
                    candidates.append(
                        context.candidate(
                            RuleId.R003_UNREACHABLE_AFTER_RETURN,
                            line_start=first.lineno,
                            line_end=last.end_lineno or last.lineno,
                            column=first.col_offset,
                            message=(
                                f"Unreachable code after {terminator} on line "
                                f"{statement.lineno}."
                            ),
                        )
                    )
                return
            for child_list in _statement_lists(statement):
                visit(child_list)

    visit(context.tree.body)
    return candidates


def _is_pass_like(body: list[ast.stmt]) -> bool:
    if not body:
        return False
    for statement in body:
        if isinstance(statement, ast.Pass):
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            if statement.value.value is Ellipsis or isinstance(statement.value.value, str):
                continue
        return False
    return True


def check_r004(context: RuleContext) -> list[RuleCandidate]:
    candidates: list[RuleCandidate] = []
    for node in ast.walk(context.tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is not None:
            continue
        if _is_pass_like(node.body):
            message = (
                "Bare except catches BaseException and swallows the exception "
                "(pass/...)."
            )
        else:
            message = (
                "Bare except catches BaseException without narrowing the "
                "exception type."
            )
        candidates.append(
            context.candidate(
                RuleId.R004_BARE_EXCEPT_SWALLOW,
                line_start=node.lineno,
                line_end=node.lineno,
                column=node.col_offset,
                message=message,
            )
        )
    return candidates


_UNBOUND = object()


def _guard_effects(test: ast.AST) -> tuple[set[str], set[str]]:
    """Return names known safe/unsafe when the test is true."""
    if isinstance(test, ast.Name):
        return {test.id}, set()
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        safe_true, safe_false = _guard_effects(test.operand)
        return safe_false, safe_true
    if isinstance(test, ast.BoolOp):
        safe_true: set[str] = set()
        safe_false: set[str] = set()
        for value in test.values:
            value_true, value_false = _guard_effects(value)
            if isinstance(test.op, ast.And):
                safe_true.update(value_true)
            elif isinstance(test.op, ast.Or):
                safe_false.update(value_false)
        return safe_true, safe_false
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and len(test.comparators) == 1
    ):
        left = test.left
        right = test.comparators[0]
        operation = test.ops[0]
        name: str | None = None
        if (
            isinstance(left, ast.Name)
            and isinstance(right, ast.Constant)
            and right.value is None
        ):
            name = left.id
        elif (
            isinstance(right, ast.Name)
            and isinstance(left, ast.Constant)
            and left.value is None
        ):
            name = right.id
        if name is not None:
            if isinstance(operation, (ast.IsNot, ast.NotEq)):
                return {name}, set()
            if isinstance(operation, (ast.Is, ast.Eq)):
                return set(), {name}
    return set(), set()


def _pattern_bound_names(pattern: ast.pattern) -> set[str]:
    if isinstance(pattern, ast.MatchAs):
        names = {pattern.name} if pattern.name else set()
        if pattern.pattern is not None:
            names.update(_pattern_bound_names(pattern.pattern))
        return names
    if isinstance(pattern, ast.MatchStar):
        return {pattern.name} if pattern.name else set()
    if isinstance(pattern, ast.MatchMapping):
        names = {pattern.rest} if pattern.rest else set()
        for subpattern in pattern.patterns:
            names.update(_pattern_bound_names(subpattern))
        return names
    if isinstance(pattern, ast.MatchSequence):
        names: set[str] = set()
        for subpattern in pattern.patterns:
            names.update(_pattern_bound_names(subpattern))
        return names
    if isinstance(pattern, ast.MatchClass):
        names: set[str] = set()
        for subpattern in list(pattern.patterns) + list(pattern.kwd_patterns):
            names.update(_pattern_bound_names(subpattern))
        return names
    if isinstance(pattern, ast.MatchOr):
        names: set[str] = set()
        for subpattern in pattern.patterns:
            names.update(_pattern_bound_names(subpattern))
        return names
    return set()


class _NoneDerefAnalyzer:
    def __init__(self, context: RuleContext) -> None:
        self.context = context
        self.candidates: list[RuleCandidate] = []

    def analyze(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> list[RuleCandidate]:
        self._walk_statements(node.body, {})
        return self.candidates

    def _record(self, name: str, origin: int, node: ast.AST) -> None:
        line = getattr(node, "lineno", 1)
        column = getattr(node, "col_offset", 0)
        self.candidates.append(
            self.context.candidate(
                RuleId.R002_NONE_DEREF_NO_GUARD,
                line_start=line,
                line_end=line,
                column=column,
                message=(
                    f"`{name}` was assigned None on line {origin} and is "
                    "dereferenced here without a guard."
                ),
            )
        )

    def _walk_statements(
        self,
        statements: list[ast.stmt],
        state: dict[str, int],
    ) -> None:
        for statement in statements:
            if isinstance(statement, ast.Assign):
                self._visit_expr(statement.value, state)
                self._bind_assignment(statement.targets, statement.value, state)
            elif isinstance(statement, ast.AnnAssign):
                if statement.value is not None:
                    self._visit_expr(statement.value, state)
                self._bind_annotation(statement.target, statement.value, state)
            elif isinstance(statement, ast.AugAssign):
                self._visit_expr(statement.target, state)
                self._visit_expr(statement.value, state)
                if isinstance(statement.target, ast.Name):
                    state.pop(statement.target.id, None)
            elif isinstance(statement, ast.Expr):
                self._visit_expr(statement.value, state)
            elif isinstance(statement, ast.Return):
                if statement.value is not None:
                    self._visit_expr(statement.value, state)
                return
            elif isinstance(statement, ast.Raise):
                if statement.exc is not None:
                    self._visit_expr(statement.exc, state)
                if statement.cause is not None:
                    self._visit_expr(statement.cause, state)
                return
            elif isinstance(statement, ast.Assert):
                self._visit_expr(statement.test, state)
                if statement.msg is not None:
                    self._visit_expr(statement.msg, state)
                safe_true, _ = _guard_effects(statement.test)
                for name in safe_true:
                    state.pop(name, None)
            elif isinstance(statement, ast.If):
                self._visit_expr(statement.test, state)
                safe_true, safe_false = _guard_effects(statement.test)
                body_state = {
                    name: origin
                    for name, origin in state.items()
                    if name not in safe_true
                }
                else_state = {
                    name: origin
                    for name, origin in state.items()
                    if name not in safe_false
                }
                self._walk_statements(statement.body, body_state)
                self._walk_statements(statement.orelse, else_state)
                state.clear()
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                self._visit_expr(statement.iter, state)
                body_state = dict(state)
                self._bind_target(statement.target, body_state)
                self._walk_statements(statement.body, body_state)
                self._walk_statements(statement.orelse, dict(state))
                state.clear()
            elif isinstance(statement, ast.While):
                self._visit_expr(statement.test, state)
                self._walk_statements(statement.body, dict(state))
                self._walk_statements(statement.orelse, dict(state))
                state.clear()
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    self._visit_expr(item.context_expr, state)
                    if item.optional_vars is not None:
                        self._bind_target(item.optional_vars, state)
                self._walk_statements(statement.body, dict(state))
                state.clear()
            elif isinstance(statement, ast.Try):
                self._walk_statements(statement.body, dict(state))
                for handler in statement.handlers:
                    handler_state = dict(state)
                    if handler.type is not None:
                        self._visit_expr(handler.type, handler_state)
                    if handler.name:
                        handler_state.pop(handler.name, None)
                    self._walk_statements(handler.body, handler_state)
                self._walk_statements(statement.orelse, dict(state))
                self._walk_statements(statement.finalbody, dict(state))
                state.clear()
            elif isinstance(statement, ast.Match):
                self._visit_expr(statement.subject, state)
                for case in statement.cases:
                    case_state = dict(state)
                    for name in _pattern_bound_names(case.pattern):
                        case_state.pop(name, None)
                    if case.guard is not None:
                        self._visit_expr(case.guard, case_state)
                    self._walk_statements(case.body, case_state)
                state.clear()
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                state.pop(statement.name, None)
            elif isinstance(statement, ast.ClassDef):
                state.pop(statement.name, None)
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    state.pop(bound, None)
            elif isinstance(statement, ast.ImportFrom):
                for alias in statement.names:
                    if alias.name == "*":
                        continue
                    state.pop(alias.asname or alias.name, None)
            elif isinstance(statement, ast.Delete):
                for target in statement.targets:
                    self._visit_expr(target, state)
                    if isinstance(target, ast.Name):
                        state.pop(target.id, None)
            elif isinstance(statement, (ast.Break, ast.Continue)):
                return

    def _bind_assignment(
        self,
        targets: list[ast.expr],
        value: ast.expr,
        state: dict[str, int],
    ) -> None:
        none_value = isinstance(value, ast.Constant) and value.value is None
        origin = getattr(value, "lineno", 0)
        for target in targets:
            if isinstance(target, ast.Name):
                if none_value:
                    state[target.id] = origin
                else:
                    state.pop(target.id, None)
            else:
                self._visit_expr(target, state)
                for name in _target_names(target):
                    state.pop(name, None)

    def _bind_annotation(
        self,
        target: ast.expr,
        value: ast.expr | None,
        state: dict[str, int],
    ) -> None:
        if isinstance(target, ast.Name):
            if value is None:
                return
            if isinstance(value, ast.Constant) and value.value is None:
                state[target.id] = getattr(value, "lineno", 0)
            else:
                state.pop(target.id, None)
            return
        self._visit_expr(target, state)
        for name in _target_names(target):
            state.pop(name, None)

    def _bind_target(self, target: ast.expr, state: dict[str, int]) -> None:
        self._visit_expr(target, state)
        for name in _target_names(target):
            state.pop(name, None)

    def _visit_expr(self, node: ast.AST | None, state: dict[str, int]) -> None:
        if node is None:
            return
        if isinstance(node, ast.Name):
            return
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                origin = state.get(node.value.id)
                if origin is not None:
                    self._record(node.value.id, origin, node)
            self._visit_expr(node.value, state)
            return
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name):
                origin = state.get(node.value.id)
                if origin is not None:
                    self._record(node.value.id, origin, node)
            self._visit_expr(node.value, state)
            self._visit_expr(node.slice, state)
            return
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                origin = state.get(node.func.id)
                if origin is not None:
                    self._record(node.func.id, origin, node)
            self._visit_expr(node.func, state)
            for argument in node.args:
                self._visit_expr(argument, state)
            for keyword in node.keywords:
                self._visit_expr(keyword.value, state)
            return
        if isinstance(node, ast.BoolOp):
            current = dict(state)
            for value in node.values:
                self._visit_expr(value, current)
                safe_true, safe_false = _guard_effects(value)
                names = safe_true if isinstance(node.op, ast.And) else safe_false
                for name in names:
                    current.pop(name, None)
            return
        if isinstance(node, ast.IfExp):
            self._visit_expr(node.test, state)
            safe_true, safe_false = _guard_effects(node.test)
            body_state = {
                name: origin
                for name, origin in state.items()
                if name not in safe_true
            }
            else_state = {
                name: origin
                for name, origin in state.items()
                if name not in safe_false
            }
            self._visit_expr(node.body, body_state)
            self._visit_expr(node.orelse, else_state)
            return
        if isinstance(node, ast.NamedExpr):
            self._visit_expr(node.value, state)
            if isinstance(node.target, ast.Name):
                if isinstance(node.value, ast.Constant) and node.value.value is None:
                    state[node.target.id] = getattr(node.value, "lineno", 0)
                else:
                    state.pop(node.target.id, None)
            return
        if isinstance(node, ast.Lambda):
            for default in node.args.defaults:
                self._visit_expr(default, state)
            for default in node.args.kw_defaults:
                if default is not None:
                    self._visit_expr(default, state)
            return
        if isinstance(
            node,
            (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp),
        ):
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._visit_expr(child, state)


def check_r002(context: RuleContext) -> list[RuleCandidate]:
    candidates: list[RuleCandidate] = []
    for node in ast.walk(context.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            analyzer = _NoneDerefAnalyzer(context)
            candidates.extend(analyzer.analyze(node))
    return candidates


RESOURCE_CALLS = frozenset(
    {
        "builtins.open",
        "io.open",
        "codecs.open",
        "gzip.open",
        "bz2.open",
        "lzma.open",
        "tarfile.open",
        "zipfile.ZipFile",
        "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryFile",
        "tempfile.SpooledTemporaryFile",
        "sqlite3.connect",
        "socket.socket",
        "socket.create_connection",
        "urllib.request.urlopen",
    }
)


class _Scope:
    def __init__(self, parent: _Scope | None = None) -> None:
        self.parent = parent
        self.bindings: dict[str, str | None] = {}
        self.global_names: set[str] = set()
        self.nonlocal_names: set[str] = set()

    def root(self) -> _Scope:
        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope

    def bind_local(self, name: str) -> None:
        self.bindings[name] = None

    def bind_import(self, name: str, qualified: str) -> None:
        # A non-import local binding always wins over an import alias.
        if name in self.bindings and self.bindings[name] is None:
            return
        self.bindings[name] = qualified

    def resolve(self, name: str) -> object:
        scope: _Scope | None = self
        if name in self.global_names:
            scope = self.root()
        elif name in self.nonlocal_names:
            scope = self.parent
        while scope is not None:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return _UNBOUND


class _BindingCollector(ast.NodeVisitor):
    def __init__(self, scope: _Scope) -> None:
        self.scope = scope

    def _bind_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            if (
                target.id in self.scope.global_names
                or target.id in self.scope.nonlocal_names
            ):
                return
            self.scope.bind_local(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind_target(element)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            if alias.asname:
                self.scope.bind_import(alias.asname, alias.name)
            else:
                root = alias.name.split(".")[0]
                self.scope.bind_import(root, root)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            module = "." * node.level + (node.module or "")
        else:
            module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                continue
            qualified = f"{module}.{alias.name}" if module else alias.name
            self.scope.bind_import(alias.asname or alias.name, qualified)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind_target(target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind_target(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._bind_target(node.target)
        self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:
        self._bind_target(node.target)
        self.visit(node.iter)
        for statement in node.body:
            self.visit(statement)
        for statement in node.orelse:
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        if node.name:
            self.scope.bind_local(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_Global(self, node: ast.Global) -> None:
        self.scope.global_names.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.scope.nonlocal_names.update(node.names)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind_target(node.target)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._bind_target(target)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.bind_local(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.bind_local(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def visit_ListComp(self, node: ast.ListComp) -> None:
        return

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            for name in _pattern_bound_names(case.pattern):
                self.scope.bind_local(name)
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)


class _ResourceCallAnalyzer:
    def __init__(self, context: RuleContext) -> None:
        self.context = context
        self.candidates: list[RuleCandidate] = []

    def run(self, tree: ast.Module) -> list[RuleCandidate]:
        module_scope = _Scope()
        collector = _BindingCollector(module_scope)
        for statement in tree.body:
            collector.visit(statement)
        self._visit_statements(tree.body, module_scope)
        return self.candidates

    def _visit_statements(
        self,
        statements: list[ast.stmt],
        scope: _Scope,
    ) -> None:
        for statement in statements:
            self._visit_statement(statement, scope)

    def _visit_statement(self, statement: ast.stmt, scope: _Scope) -> None:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._visit_function(statement, scope, scope)
            return
        if isinstance(statement, ast.ClassDef):
            self._visit_class(statement, scope)
            return
        if isinstance(statement, ast.Assign):
            self._visit_expression(statement.value, scope)
            for target in statement.targets:
                self._visit_expression(target, scope)
            return
        if isinstance(statement, ast.AnnAssign):
            if statement.value is not None:
                self._visit_expression(statement.value, scope)
            self._visit_expression(statement.target, scope)
            return
        if isinstance(statement, ast.AugAssign):
            self._visit_expression(statement.target, scope)
            self._visit_expression(statement.value, scope)
            return
        if isinstance(statement, ast.Expr):
            self._visit_expression(statement.value, scope)
            return
        if isinstance(statement, ast.Return):
            if statement.value is not None:
                self._visit_expression(statement.value, scope)
            return
        if isinstance(statement, ast.Raise):
            if statement.exc is not None:
                self._visit_expression(statement.exc, scope)
            if statement.cause is not None:
                self._visit_expression(statement.cause, scope)
            return
        if isinstance(statement, ast.Assert):
            self._visit_expression(statement.test, scope)
            if statement.msg is not None:
                self._visit_expression(statement.msg, scope)
            return
        if isinstance(statement, ast.Delete):
            for target in statement.targets:
                self._visit_expression(target, scope)
            return
        if isinstance(statement, (ast.If, ast.While)):
            self._visit_expression(statement.test, scope)
            self._visit_statements(statement.body, scope)
            self._visit_statements(statement.orelse, scope)
            return
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            self._visit_expression(statement.iter, scope)
            self._visit_expression(statement.target, scope)
            self._visit_statements(statement.body, scope)
            self._visit_statements(statement.orelse, scope)
            return
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                self._visit_expression(item.context_expr, scope, managed=True)
                if item.optional_vars is not None:
                    self._visit_expression(item.optional_vars, scope)
            self._visit_statements(statement.body, scope)
            return
        if isinstance(statement, ast.Try):
            self._visit_statements(statement.body, scope)
            for handler in statement.handlers:
                if handler.type is not None:
                    self._visit_expression(handler.type, scope)
                self._visit_statements(handler.body, scope)
            self._visit_statements(statement.orelse, scope)
            self._visit_statements(statement.finalbody, scope)
            return
        if isinstance(statement, ast.Match):
            self._visit_expression(statement.subject, scope)
            for case in statement.cases:
                if case.guard is not None:
                    self._visit_expression(case.guard, scope)
                self._visit_statements(case.body, scope)
            return

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        default_scope: _Scope,
        body_parent_scope: _Scope,
    ) -> None:
        for decorator in node.decorator_list:
            self._visit_expression(decorator, default_scope)
        for default in node.args.defaults:
            self._visit_expression(default, default_scope)
        for default in node.args.kw_defaults:
            if default is not None:
                self._visit_expression(default, default_scope)

        arguments = (
            list(node.args.posonlyargs)
            + list(node.args.args)
            + list(node.args.kwonlyargs)
        )
        for argument in arguments:
            if argument.annotation is not None:
                self._visit_expression(argument.annotation, default_scope)
        if node.args.vararg is not None and node.args.vararg.annotation is not None:
            self._visit_expression(node.args.vararg.annotation, default_scope)
        if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
            self._visit_expression(node.args.kwarg.annotation, default_scope)
        if node.returns is not None:
            self._visit_expression(node.returns, default_scope)

        local_scope = _Scope(parent=body_parent_scope)
        for argument in arguments:
            local_scope.bind_local(argument.arg)
        if node.args.vararg is not None:
            local_scope.bind_local(node.args.vararg.arg)
        if node.args.kwarg is not None:
            local_scope.bind_local(node.args.kwarg.arg)

        collector = _BindingCollector(local_scope)
        for statement in node.body:
            collector.visit(statement)
        self._visit_statements(node.body, local_scope)

    def _visit_class(self, node: ast.ClassDef, scope: _Scope) -> None:
        for decorator in node.decorator_list:
            self._visit_expression(decorator, scope)
        for base in node.bases:
            self._visit_expression(base, scope)
        for keyword in node.keywords:
            self._visit_expression(keyword.value, scope)

        class_scope = _Scope(parent=scope)
        collector = _BindingCollector(class_scope)
        for statement in node.body:
            collector.visit(statement)

        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Defaults resolve in the class body; the method body does not
                # close over the class namespace.
                self._visit_function(statement, class_scope, scope)
            elif isinstance(statement, ast.ClassDef):
                self._visit_class(statement, class_scope)
            else:
                self._visit_statement(statement, class_scope)

    def _visit_expression(
        self,
        node: ast.AST | None,
        scope: _Scope,
        *,
        managed: bool = False,
    ) -> None:
        if node is None:
            return
        if isinstance(node, ast.Call):
            if not managed:
                qualified = self._qualified_name(node, scope)
                if qualified in RESOURCE_CALLS:
                    self.candidates.append(
                        self.context.candidate(
                            RuleId.R005_RESOURCE_NO_WITH,
                            line_start=node.lineno,
                            line_end=node.end_lineno or node.lineno,
                            column=node.col_offset,
                            message=(
                                f"`{qualified}` result is not managed by a "
                                "with block."
                            ),
                        )
                    )
            self._visit_expression(node.func, scope, managed=managed)
            for argument in node.args:
                self._visit_expression(argument, scope, managed=managed)
            for keyword in node.keywords:
                self._visit_expression(keyword.value, scope, managed=managed)
            return
        if isinstance(node, ast.Lambda):
            for default in node.args.defaults:
                self._visit_expression(default, scope, managed=managed)
            for default in node.args.kw_defaults:
                if default is not None:
                    self._visit_expression(default, scope, managed=managed)
            lambda_scope = _Scope(parent=scope)
            arguments = (
                list(node.args.posonlyargs)
                + list(node.args.args)
                + list(node.args.kwonlyargs)
            )
            for argument in arguments:
                lambda_scope.bind_local(argument.arg)
            if node.args.vararg is not None:
                lambda_scope.bind_local(node.args.vararg.arg)
            if node.args.kwarg is not None:
                lambda_scope.bind_local(node.args.kwarg.arg)
            self._visit_expression(node.body, lambda_scope, managed=managed)
            return
        if isinstance(
            node,
            (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp),
        ):
            comprehension_scope = _Scope(parent=scope)
            for index, generator in enumerate(node.generators):
                self._visit_expression(
                    generator.iter,
                    scope if index == 0 else comprehension_scope,
                    managed=managed,
                )
                for name in _target_names(generator.target):
                    comprehension_scope.bind_local(name)
                for condition in generator.ifs:
                    self._visit_expression(
                        condition,
                        comprehension_scope,
                        managed=managed,
                    )
            if isinstance(node, ast.DictComp):
                self._visit_expression(node.key, comprehension_scope, managed=managed)
                self._visit_expression(node.value, comprehension_scope, managed=managed)
            else:
                self._visit_expression(node.elt, comprehension_scope, managed=managed)
            return
        if isinstance(node, ast.Attribute):
            self._visit_expression(node.value, scope, managed=managed)
            return
        if isinstance(node, ast.Subscript):
            self._visit_expression(node.value, scope, managed=managed)
            self._visit_expression(node.slice, scope, managed=managed)
            return
        if isinstance(node, ast.NamedExpr):
            self._visit_expression(node.value, scope, managed=managed)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._visit_expression(child, scope, managed=managed)

    def _qualified_name(self, call: ast.Call, scope: _Scope) -> str | None:
        parts: list[str] = []
        node = call.func
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        base = scope.resolve(node.id)
        if base is _UNBOUND:
            if node.id == "open" and not parts:
                return "builtins.open"
            return None
        if base is None:
            return None
        if not parts:
            return base
        return ".".join([base, *reversed(parts)])


def check_r005(context: RuleContext) -> list[RuleCandidate]:
    return _ResourceCallAnalyzer(context).run(context.tree)


RuleHandler = Callable[[RuleContext], list[RuleCandidate]]

RULE_HANDLERS: dict[RuleId, RuleHandler] = {
    RuleId.R001_MUTABLE_DEFAULT: check_r001,
    RuleId.R002_NONE_DEREF_NO_GUARD: check_r002,
    RuleId.R003_UNREACHABLE_AFTER_RETURN: check_r003,
    RuleId.R004_BARE_EXCEPT_SWALLOW: check_r004,
    RuleId.R005_RESOURCE_NO_WITH: check_r005,
}


def scan_tree(
    module: LoadedModule,
    rules: tuple[RuleId, ...] = ALL_RULES,
) -> list[RuleCandidate]:
    requested = set(rules)
    unimplemented = [rule for rule in requested if rule not in RULE_HANDLERS]
    if unimplemented:
        names = ", ".join(rule.value for rule in unimplemented)
        raise ValueError(f"rules not implemented: {names}")

    context = RuleContext(
        file=module.file,
        relative_file=module.relative_file,
        source=module.source,
        tree=module.tree,
    )
    candidates: list[RuleCandidate] = []
    for rule in ALL_RULES:
        if rule not in requested:
            continue
        candidates.extend(RULE_HANDLERS[rule](context))
    candidates.sort(
        key=lambda candidate: (
            candidate.relative_file,
            candidate.line_start,
            candidate.line_end,
            candidate.rule_id.value,
            candidate.column,
            candidate.finding_id,
        )
    )
    return candidates
