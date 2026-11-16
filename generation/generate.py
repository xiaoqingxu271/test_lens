"""Candidate test generation behind the LLM interface.

The model only proposes tests. This stage decides what is structurally
admissible: parseable source, one test function matching the declared name, a
safe relative test path, a reference to the selected target, and no duplicate
or colliding file. Nothing here executes the target project or writes a file;
the sandbox executor materializes and runs accepted candidates later.

The stage refuses to run unless the sandbox acceptance probe is ready.
"""

from __future__ import annotations

import ast
import json
import time
import tokenize
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from llm.base import LLMClient
from llm.models import LLMPurpose, LLMRequest, LLMResponse, ModelKind
from sandbox.health import SandboxHealth, check_sandbox_ready
from sandbox.runner import ProjectProfile, SandboxConfig
from targets.models import SelectedTarget, TargetSelectionReport

from .models import (
    GeneratedTest,
    GenerationRejectReason,
    GenerationReport,
    GenerationTrace,
    RejectedProposal,
)

GENERATION_TEMPLATE_VERSION = "generation-v1"
GENERATION_SYSTEM_PROMPT = (
    "You propose pytest test functions for exactly one Python route handler. "
    'Return one JSON object: {"tests": [{"test_file": str, "test_name": str, '
    '"source": str}]}. Use a relative POSIX path ending in .py for test_file. '
    "The source must define exactly one top-level function named test_name. "
    "Do not modify project files and do not add tests for other symbols."
)


class SandboxUnavailableError(RuntimeError):
    """Raised when test generation is entered without a ready sandbox."""

    def __init__(self, health: SandboxHealth) -> None:
        self.health = health
        failures = ", ".join(failure.value for failure in health.failures)
        super().__init__(f"sandbox is not ready: {failures or 'probe failed'}")


def module_name(relative_file: str) -> str:
    path = PurePosixPath(relative_file)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def generation_request_id(symbol_id: str) -> str:
    return f"{LLMPurpose.TEST_GENERATION.value}::{symbol_id}"


def _parse_function(source_slice: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    try:
        tree = ast.parse(source_slice)
    except (SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _parameter_context(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[dict[str, object]]:
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    default_start = len(positional) - len(args.defaults)

    parameters: list[dict[str, object]] = []
    for index, argument in enumerate(positional):
        parameters.append(
            {
                "name": argument.arg,
                "has_default": index >= default_start,
                "kind": (
                    "positional_only"
                    if index < len(args.posonlyargs)
                    else "positional_or_keyword"
                ),
            }
        )
    if args.vararg is not None:
        parameters.append(
            {"name": args.vararg.arg, "has_default": False, "kind": "var_positional"}
        )
    for argument, default in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(
            {
                "name": argument.arg,
                "has_default": default is not None,
                "kind": "keyword_only",
            }
        )
    if args.kwarg is not None:
        parameters.append(
            {"name": args.kwarg.arg, "has_default": False, "kind": "var_keyword"}
        )
    return parameters


def build_generation_prompt(target: SelectedTarget, source_slice: str) -> str:
    function = _parse_function(source_slice)
    context = {
        "symbol_id": target.symbol_id,
        "module": module_name(target.relative_file),
        "name": target.name,
        "file": target.relative_file,
        "line_start": target.line_start,
        "line_end": target.line_end,
        "is_async": isinstance(function, ast.AsyncFunctionDef),
        "parameters": _parameter_context(function) if function is not None else [],
        "routes": [
            {"methods": route.methods, "path": route.path, "decorator": route.decorator}
            for route in target.routes
        ],
        "source": source_slice,
    }
    return (
        "Propose pytest tests for the target below. Return JSON only.\n"
        "<TARGET_JSON>\n"
        f"{json.dumps(context, sort_keys=True, indent=2)}\n"
        "</TARGET_JSON>\n"
    )


def _target_source(
    root: Path, target: SelectedTarget
) -> tuple[str | None, str | None]:
    path = Path(target.file)
    if not path.is_absolute():
        path = root / path
    try:
        with tokenize.open(path) as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return None, f"{type(exc).__name__}: {exc}"

    lines = text.splitlines()
    start = max(target.line_start - 1, 0)
    end = min(target.line_end, len(lines))
    if start >= len(lines):
        return None, "target span lies outside the file"
    return "\n".join(lines[start:end]), None


def _normalized_test_path(test_file: str) -> PurePosixPath | None:
    if not test_file or not test_file.strip():
        return None
    if "\\" in test_file or "\x00" in test_file:
        return None
    path = PurePosixPath(test_file)
    if path.is_absolute():
        return None
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    if path.suffix != ".py":
        return None
    name = path.name
    if not (name.startswith("test_") or name.endswith("_test.py")):
        return None
    return path


def _references_target(
    tree: ast.Module, target: SelectedTarget
) -> bool:
    module = module_name(target.relative_file)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == module:
                return True
            if node.module and module.startswith(f"{node.module}."):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module or module.startswith(f"{alias.name}."):
                    return True
        elif isinstance(node, ast.Name):
            if node.id == target.name:
                return True
        elif isinstance(node, ast.Attribute):
            if node.attr == target.name:
                return True
    return False


def _proposal_items(payload: object) -> list[object] | None:
    if not isinstance(payload, dict):
        return None
    tests = payload.get("tests")
    return tests if isinstance(tests, list) else None


def _rejection(
    target: SelectedTarget,
    *,
    reason: GenerationRejectReason,
    detail: str,
    model_kind: ModelKind,
    model_id: str,
    prompt_digest: str = "",
    response_digest: str = "",
    test_file: str | None = None,
    test_name: str | None = None,
) -> RejectedProposal:
    return RejectedProposal(
        target_symbol=target.symbol_id,
        reason=reason,
        detail=detail,
        prompt_digest=prompt_digest,
        response_digest=response_digest,
        model_kind=model_kind,
        model_id=model_id,
        test_file=test_file,
        test_name=test_name,
    )


def _validate_proposal(
    item: object,
    target: SelectedTarget,
    request: LLMRequest,
    response: LLMResponse,
    root: Path,
    seen: dict[str, str],
) -> GeneratedTest | RejectedProposal:
    def reject(
        reason: GenerationRejectReason,
        detail: str,
        test_file: str | None = None,
        test_name: str | None = None,
    ) -> RejectedProposal:
        return _rejection(
            target,
            reason=reason,
            detail=detail,
            model_kind=response.model_kind,
            model_id=response.model_id,
            prompt_digest=request.prompt_digest,
            response_digest=response.response_digest,
            test_file=test_file,
            test_name=test_name,
        )

    if not isinstance(item, dict):
        return reject(
            GenerationRejectReason.INVALID_SHAPE,
            "each proposal must be an object",
        )

    test_file = item.get("test_file")
    test_name = item.get("test_name")
    source = item.get("source")
    if not all(isinstance(value, str) for value in (test_file, test_name, source)):
        return reject(
            GenerationRejectReason.INVALID_SHAPE,
            "test_file, test_name and source must be strings",
        )

    if not test_name.startswith("test_"):
        return reject(
            GenerationRejectReason.NO_TEST_FUNCTION,
            "test_name must start with 'test_'",
            test_file,
            test_name,
        )

    path = _normalized_test_path(test_file)
    if path is None:
        return reject(
            GenerationRejectReason.INVALID_TEST_PATH,
            "test_file must be a relative POSIX path to a test_*.py file",
            test_file,
            test_name,
        )
    normalized = path.as_posix()

    if (root / path).exists():
        return reject(
            GenerationRejectReason.FILE_EXISTS,
            f"{normalized} already exists in the project",
            normalized,
            test_name,
        )

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return reject(
            GenerationRejectReason.SYNTAX_ERROR,
            f"{type(exc).__name__}: {exc}",
            normalized,
            test_name,
        )

    functions = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if len(functions) != 1 or functions[0].name != test_name:
        return reject(
            GenerationRejectReason.NO_TEST_FUNCTION,
            "source must define exactly one top-level function named test_name",
            normalized,
            test_name,
        )

    if not _references_target(tree, target):
        return reject(
            GenerationRejectReason.TARGET_MISMATCH,
            f"test does not import or reference {target.symbol_id}",
            normalized,
            test_name,
        )

    test_id = f"{normalized}::{test_name}"
    if test_id in seen:
        return reject(
            GenerationRejectReason.DUPLICATE_TEST_ID,
            f"duplicate of the proposal for {seen[test_id]}",
            normalized,
            test_name,
        )

    return GeneratedTest(
        test_id=test_id,
        target_symbol=target.symbol_id,
        test_file=normalized,
        test_name=test_name,
        source=source,
        prompt_digest=request.prompt_digest,
        response_digest=response.response_digest,
        prompt_template_version=request.prompt_template_version,
        model_kind=response.model_kind,
        model_id=response.model_id,
    )


def generate_tests(
    profile: ProjectProfile,
    selection: TargetSelectionReport,
    llm: LLMClient,
    *,
    sandbox_health: SandboxHealth | None = None,
    health_timeout_seconds: float = 30.0,
    sandbox_config: SandboxConfig | None = None,
) -> GenerationReport:
    """Propose, validate, and enumerate candidate tests for selected targets."""
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    if sandbox_health is None:
        sandbox_health = check_sandbox_ready(
            timeout_seconds=health_timeout_seconds,
            config=sandbox_config,
        )
    if not sandbox_health.ready:
        raise SandboxUnavailableError(sandbox_health)

    root = Path(profile.root).expanduser().resolve()
    candidates: list[GeneratedTest] = []
    rejected: list[RejectedProposal] = []
    seen: dict[str, str] = {}
    counts: Counter[GenerationRejectReason] = Counter()
    warnings: set[str] = set()
    tests_proposed = 0

    for target in selection.targets:
        source_slice, read_error = _target_source(root, target)
        if read_error is not None or source_slice is None:
            rejected.append(
                _rejection(
                    target,
                    reason=GenerationRejectReason.SOURCE_UNREADABLE,
                    detail=read_error or "target source could not be read",
                    model_kind=llm.model_kind,
                    model_id=llm.model_id,
                )
            )
            counts[GenerationRejectReason.SOURCE_UNREADABLE] += 1
            continue

        prompt = build_generation_prompt(target, source_slice)
        request = LLMRequest(
            request_id=generation_request_id(target.symbol_id),
            purpose=LLMPurpose.TEST_GENERATION,
            prompt_template_version=GENERATION_TEMPLATE_VERSION,
            system=GENERATION_SYSTEM_PROMPT,
            prompt=prompt,
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

        text = response.text
        if not text.strip():
            rejected.append(
                _rejection(
                    target,
                    reason=GenerationRejectReason.EMPTY_RESPONSE,
                    detail="model returned an empty response",
                    model_kind=response.model_kind,
                    model_id=response.model_id,
                    prompt_digest=request.prompt_digest,
                    response_digest=response.response_digest,
                )
            )
            counts[GenerationRejectReason.EMPTY_RESPONSE] += 1
            continue

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            rejected.append(
                _rejection(
                    target,
                    reason=GenerationRejectReason.INVALID_JSON,
                    detail=f"JSONDecodeError: {exc.msg}",
                    model_kind=response.model_kind,
                    model_id=response.model_id,
                    prompt_digest=request.prompt_digest,
                    response_digest=response.response_digest,
                )
            )
            counts[GenerationRejectReason.INVALID_JSON] += 1
            continue

        tests = _proposal_items(payload)
        if tests is None:
            rejected.append(
                _rejection(
                    target,
                    reason=GenerationRejectReason.INVALID_SHAPE,
                    detail='response must be an object with a "tests" list',
                    model_kind=response.model_kind,
                    model_id=response.model_id,
                    prompt_digest=request.prompt_digest,
                    response_digest=response.response_digest,
                )
            )
            counts[GenerationRejectReason.INVALID_SHAPE] += 1
            continue

        if not tests:
            rejected.append(
                _rejection(
                    target,
                    reason=GenerationRejectReason.NO_PROPOSAL,
                    detail="model proposed no tests for this target",
                    model_kind=response.model_kind,
                    model_id=response.model_id,
                    prompt_digest=request.prompt_digest,
                    response_digest=response.response_digest,
                )
            )
            counts[GenerationRejectReason.NO_PROPOSAL] += 1
            continue

        for item in tests:
            tests_proposed += 1
            outcome = _validate_proposal(item, target, request, response, root, seen)
            if isinstance(outcome, GeneratedTest):
                candidates.append(outcome)
                seen[outcome.test_id] = target.symbol_id
            else:
                rejected.append(outcome)
                counts[outcome.reason] += 1

    if not selection.targets:
        warnings.add("no targets selected")

    candidates.sort(
        key=lambda candidate: (
            candidate.target_symbol,
            candidate.test_file,
            candidate.test_name,
        )
    )
    rejected.sort(
        key=lambda item: (
            item.target_symbol,
            item.test_file or "",
            item.test_name or "",
            item.reason.value,
        )
    )

    trace = GenerationTrace(
        started_at=started_at,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        llm_model_kind=llm.model_kind,
        llm_model_id=llm.model_id,
        prompt_template_version=GENERATION_TEMPLATE_VERSION,
        targets_considered=len(selection.targets),
        tests_proposed=tests_proposed,
        tests_accepted=len(candidates),
        tests_rejected=len(rejected),
        rejected_by_reason=dict(
            sorted(counts.items(), key=lambda item: item[0].value)
        ),
        sandbox_health=sandbox_health,
        warnings=sorted(warnings),
    )
    return GenerationReport(candidates=candidates, rejected=rejected, trace=trace)
