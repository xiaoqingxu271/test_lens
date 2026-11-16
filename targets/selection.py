"""Deterministic AST-based target selection for FastAPI/Flask-style routes."""

from __future__ import annotations

import ast
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from project.files import load_modules
from sandbox.runner import ProjectProfile

from .models import (
    OrderReason,
    Route,
    RouteSkipReason,
    SelectedTarget,
    SkippedRoute,
    TargetSelectionReport,
    TargetSelectionTrace,
)


HTTP_METHOD_DECORATORS: dict[str, str] = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "patch": "PATCH",
    "options": "OPTIONS",
    "head": "HEAD",
    "trace": "TRACE",
}
ROUTE_DECORATORS = frozenset({"route", "api_route"})
UNSUPPORTED_ROUTE_DECORATORS = frozenset({"websocket"})
ALL_ROUTE_DECORATORS = (
    frozenset(HTTP_METHOD_DECORATORS) | ROUTE_DECORATORS | UNSUPPORTED_ROUTE_DECORATORS
)


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _route_style_path_keys(route_style: bool) -> tuple[str, ...]:
    return ("path", "rule") if route_style else ("path",)


def _path_argument(call: ast.Call, *, route_style: bool) -> str | None:
    if call.args:
        path = _literal_string(call.args[0])
        if path is not None:
            return path
    keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
    for key in _route_style_path_keys(route_style):
        node = keywords.get(key)
        if node is None:
            continue
        path = _literal_string(node)
        if path is not None:
            return path
    return None


def _has_path_argument(call: ast.Call, *, route_style: bool) -> bool:
    if call.args:
        return True
    keys = frozenset(_route_style_path_keys(route_style))
    return any(keyword.arg in keys for keyword in call.keywords)


def _methods_from_node(node: ast.AST | None) -> tuple[str, list[str]]:
    """Return (status, methods); status is default, ok, or dynamic."""
    if node is None:
        return "default", ["GET"]
    if isinstance(node, ast.Constant) and node.value is None:
        return "default", ["GET"]

    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = list(node.elts)
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        values = [node]
    else:
        return "dynamic", []

    methods: list[str] = []
    for value in values:
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            return "dynamic", []
        methods.append(value.value.upper())
    if not methods:
        return "dynamic", []
    return "ok", sorted(set(methods))


def _call_methods(call: ast.Call) -> tuple[str, list[str]]:
    keyword = next(
        (keyword for keyword in call.keywords if keyword.arg == "methods"),
        None,
    )
    if keyword is None:
        return "default", ["GET"]
    return _methods_from_node(keyword.value)


def _parse_route_decorator(
    decorator: ast.AST,
    *,
    relative_file: str,
) -> Route | SkippedRoute | None:
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not isinstance(func, ast.Attribute):
        return None
    attribute = func.attr
    if attribute not in ALL_ROUTE_DECORATORS:
        return None

    decorator_name = ast.unparse(func)
    line = decorator.lineno

    if attribute in UNSUPPORTED_ROUTE_DECORATORS:
        return SkippedRoute(
            file=relative_file,
            line=line,
            decorator=decorator_name,
            reason=RouteSkipReason.UNSUPPORTED_DECORATOR,
            detail=f"{attribute} routes are not selected in v1",
        )

    route_style = attribute in ROUTE_DECORATORS
    path = _path_argument(decorator, route_style=route_style)
    if path is None:
        reason = (
            RouteSkipReason.MISSING_PATH
            if not _has_path_argument(decorator, route_style=route_style)
            else RouteSkipReason.DYNAMIC_PATH
        )
        return SkippedRoute(
            file=relative_file,
            line=line,
            decorator=decorator_name,
            reason=reason,
            detail="route path must be a literal string",
        )

    if route_style:
        status, methods = _call_methods(decorator)
        if status == "dynamic":
            return SkippedRoute(
                file=relative_file,
                line=line,
                decorator=decorator_name,
                reason=RouteSkipReason.DYNAMIC_METHODS,
                detail="route methods must be literal strings",
            )
        if status == "default":
            methods = ["GET"]
    else:
        methods = [HTTP_METHOD_DECORATORS[attribute]]

    return Route(
        methods=methods,
        path=path,
        decorator=decorator_name,
        line=line,
    )


def _select_from_module(
    module,
) -> tuple[list[SelectedTarget], list[SkippedRoute], int]:
    targets: list[SelectedTarget] = []
    skipped_routes: list[SkippedRoute] = []
    functions_seen = 0

    for node in module.tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        functions_seen += 1
        routes: list[Route] = []
        for decorator in node.decorator_list:
            parsed = _parse_route_decorator(
                decorator,
                relative_file=module.relative_file,
            )
            if isinstance(parsed, Route):
                routes.append(parsed)
            elif isinstance(parsed, SkippedRoute):
                skipped_routes.append(parsed)

        if not routes:
            continue

        routes.sort(
            key=lambda route: (
                route.line,
                route.decorator,
                route.path,
                tuple(route.methods),
            )
        )
        targets.append(
            SelectedTarget(
                symbol_id=f"{module.relative_file}::{node.name}",
                name=node.name,
                file=str(module.file),
                relative_file=module.relative_file,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                routes=routes,
            )
        )

    return targets, skipped_routes, functions_seen


def _sort_targets(
    targets: list[SelectedTarget],
    coverage_rank: Mapping[str, float] | None,
) -> list[SelectedTarget]:
    if coverage_rank is None:
        return sorted(
            targets,
            key=lambda target: (
                target.relative_file,
                target.line_start,
                target.name,
            ),
        )
    return sorted(
        targets,
        key=lambda target: (
            -float(coverage_rank.get(target.symbol_id, 0.0)),
            target.relative_file,
            target.line_start,
            target.name,
        ),
    )


def select_targets(
    profile: ProjectProfile,
    *,
    coverage_rank: Mapping[str, float] | None = None,
) -> TargetSelectionReport:
    """Select route-handler targets; coverage only changes ordering."""
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    root = Path(profile.root).expanduser().resolve()
    modules, skipped_files = load_modules(
        root,
        excluded_paths=profile.test_paths,
    )

    all_targets: list[SelectedTarget] = []
    skipped_routes: list[SkippedRoute] = []
    functions_seen = 0
    for module in modules:
        module_targets, module_skipped, module_functions = _select_from_module(module)
        all_targets.extend(module_targets)
        skipped_routes.extend(module_skipped)
        functions_seen += module_functions

    targets = _sort_targets(all_targets, coverage_rank)
    skipped_routes.sort(
        key=lambda route: (
            route.file,
            route.line,
            route.decorator,
            route.reason.value,
        )
    )

    warnings: list[str] = []
    if not targets:
        warnings.append("no route handlers found")

    trace = TargetSelectionTrace(
        started_at=started_at,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        files_scanned=len(modules),
        files_skipped=skipped_files,
        functions_seen=functions_seen,
        targets_selected=len(targets),
        skipped_routes=skipped_routes,
        order_reason=(
            OrderReason.COVERAGE if coverage_rank is not None else OrderReason.STATIC
        ),
        warnings=warnings,
    )
    return TargetSelectionReport(targets=targets, trace=trace)
