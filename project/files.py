"""Deterministic project file discovery and Python source loading."""

from __future__ import annotations

import ast
import tokenize
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel


EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "tests",
        "test",
        "migrations",
        "alembic",
    }
)
EXCLUDED_FILE_NAMES = frozenset({"conftest.py", "setup.py"})
EXCLUDED_FILE_GLOBS = ("test_*.py", "*_test.py")


class SkipReason(str, Enum):
    SYNTAX_ERROR = "syntax_error"
    DECODE_ERROR = "decode_error"
    READ_ERROR = "read_error"


class SkippedFile(BaseModel):
    file: str
    reason: SkipReason
    detail: str | None = None


@dataclass(frozen=True)
class LoadedModule:
    file: Path
    relative_file: str
    source: str
    tree: ast.Module


def _resolve_excluded(root: Path, path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def _is_excluded_file(path: Path) -> bool:
    if path.name in EXCLUDED_FILE_NAMES:
        return True
    return any(path.match(pattern) for pattern in EXCLUDED_FILE_GLOBS)


def iter_python_files(
    root: Path | str,
    *,
    excluded_paths: Sequence[Path | str] = (),
) -> list[Path]:
    """Return Python files under root in deterministic relative-path order."""
    resolved_root = Path(root).expanduser().resolve()
    excluded = {_resolve_excluded(resolved_root, path) for path in excluded_paths}
    files: list[Path] = []

    candidates = sorted(
        resolved_root.rglob("*.py"),
        key=lambda path: path.relative_to(resolved_root).as_posix(),
    )
    for path in candidates:
        try:
            relative_parts = path.relative_to(resolved_root).parts
        except ValueError:
            continue
        if any(part in EXCLUDED_DIR_NAMES for part in relative_parts[:-1]):
            continue
        if path in excluded:
            continue
        if any(parent in excluded for parent in path.parents):
            continue
        if _is_excluded_file(path):
            continue
        files.append(path)
    return files


def read_source(path: Path) -> str:
    with tokenize.open(str(path)) as handle:
        return handle.read()


def load_modules(
    root: Path | str,
    *,
    excluded_paths: Sequence[Path | str] = (),
) -> tuple[list[LoadedModule], list[SkippedFile]]:
    """Parse all selected Python files, recording deterministic skip reasons."""
    resolved_root = Path(root).expanduser().resolve()
    modules: list[LoadedModule] = []
    skipped: list[SkippedFile] = []

    for path in iter_python_files(resolved_root, excluded_paths=excluded_paths):
        relative_file = path.relative_to(resolved_root).as_posix()
        try:
            source = read_source(path)
        except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
            skipped.append(
                SkippedFile(
                    file=relative_file,
                    reason=SkipReason.DECODE_ERROR,
                    detail=str(exc),
                )
            )
            continue
        except OSError as exc:
            skipped.append(
                SkippedFile(
                    file=relative_file,
                    reason=SkipReason.READ_ERROR,
                    detail=str(exc),
                )
            )
            continue

        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            skipped.append(
                SkippedFile(
                    file=relative_file,
                    reason=SkipReason.SYNTAX_ERROR,
                    detail=f"{exc.msg} (line {exc.lineno})",
                )
            )
            continue
        except ValueError as exc:
            skipped.append(
                SkippedFile(
                    file=relative_file,
                    reason=SkipReason.SYNTAX_ERROR,
                    detail=str(exc),
                )
            )
            continue

        modules.append(
            LoadedModule(
                file=path,
                relative_file=relative_file,
                source=source,
                tree=tree,
            )
        )

    return modules, skipped
