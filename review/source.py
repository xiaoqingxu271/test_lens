"""Source loading and evidence-line anchoring for cross-check."""

from __future__ import annotations

import ast
import hashlib
import tokenize
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .models import EvidenceKind, EvidenceLine, EvidenceRole


@dataclass(frozen=True)
class ReviewSource:
    absolute: Path
    relative_file: str
    text: str
    tree: ast.Module

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    def snippet(self, line_start: int, line_end: int) -> str:
        lines = self.lines
        start = max(1, line_start)
        end = max(start, line_end)
        return "\n".join(lines[start - 1 : end])


def snippet_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def claim_hash(claim: str) -> str:
    return hashlib.sha256(claim.encode("utf-8")).hexdigest()[:16]


def load_review_source(
    root: Path, relative_file: str
) -> tuple[ReviewSource | None, str | None]:
    """Read and parse a candidate file; reject paths outside the project."""
    pure = PurePosixPath(relative_file)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        return None, f"file path is not a project-relative path: {relative_file}"

    resolved_root = root.expanduser().resolve()
    path = (resolved_root / pure).resolve()
    if not path.is_relative_to(resolved_root):
        return None, f"file path escapes the project root: {relative_file}"

    try:
        with tokenize.open(path) as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return None, f"{type(exc).__name__}: {exc}"

    try:
        tree = ast.parse(text, filename=str(path))
    except (SyntaxError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"

    return (
        ReviewSource(
            absolute=path,
            relative_file=pure.as_posix(),
            text=text,
            tree=tree,
        ),
        None,
    )


def make_evidence(
    source: ReviewSource,
    line_start: int,
    line_end: int,
    *,
    kind: EvidenceKind,
    role: EvidenceRole,
    explanation: str,
) -> EvidenceLine:
    snippet = source.snippet(line_start, line_end)
    return EvidenceLine(
        file=source.relative_file,
        line_start=line_start,
        line_end=line_end,
        evidence_kind=kind,
        role=role,
        explanation=explanation,
        snippet_hash=snippet_hash(snippet),
        snippet=snippet,
    )


def line_is_located(source: ReviewSource, line_start: int, line_end: int) -> bool:
    return (
        line_start >= 1
        and line_end >= line_start
        and line_end <= len(source.lines)
    )
