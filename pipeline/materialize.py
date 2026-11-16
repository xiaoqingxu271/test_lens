"""Materialize accepted candidates into an isolated project copy.

The target project is never written to. Candidates are copied together with a
read-only copy of the project into a temporary work directory; every test file
is re-validated before it is written (relative POSIX path, no traversal, one
file per candidate).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path, PurePosixPath
from typing import Sequence

from generation.models import GeneratedTest
from sandbox.runner import ProjectProfile

from .models import MaterializedProject, MaterializedTest

COPY_IGNORES = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.pyc",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
)


class PipelineError(RuntimeError):
    """A contract violation between pipeline stages."""


def _validate_test_path(raw: str) -> PurePosixPath:
    if not raw or raw.strip() != raw or "\\" in raw or "\x00" in raw:
        raise PipelineError(f"invalid test path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute():
        raise PipelineError(f"test path must be relative: {raw!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise PipelineError(f"test path must not traverse: {raw!r}")
    if path.suffix != ".py":
        raise PipelineError(f"test path must end in .py: {raw!r}")
    name = path.name
    if not (name.startswith("test_") or name.endswith("_test.py")):
        raise PipelineError(f"test path is not a pytest file name: {raw!r}")
    return path


def materialize_tests(
    profile: ProjectProfile,
    candidates: Sequence[GeneratedTest],
    destination: Path,
    *,
    artifacts_root: Path | None = None,
) -> MaterializedProject:
    root = Path(profile.root).expanduser().resolve()
    project_root = Path(destination).expanduser().resolve()

    if project_root == root or root in project_root.parents:
        raise PipelineError("destination must live outside the target project")
    if project_root.exists():
        if any(project_root.iterdir()):
            raise PipelineError(f"destination is not empty: {project_root}")
    else:
        project_root.mkdir(parents=True)

    shutil.copytree(
        root,
        project_root,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*COPY_IGNORES),
    )

    artifacts = (
        Path(artifacts_root).expanduser().resolve()
        if artifacts_root is not None
        else project_root.parent / "artifacts"
    )
    artifacts.mkdir(parents=True, exist_ok=True)

    ordered = sorted(candidates, key=lambda candidate: candidate.test_id)
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    tests: list[MaterializedTest] = []

    for candidate in ordered:
        if candidate.test_id in seen_ids:
            raise PipelineError(f"duplicate candidate test id: {candidate.test_id}")
        seen_ids.add(candidate.test_id)

        relative = _validate_test_path(candidate.test_file)
        normalized = relative.as_posix()
        if normalized in seen_files:
            raise PipelineError(
                "multiple candidates target the same test file: " + normalized
            )
        seen_files.add(normalized)

        target_path = (project_root / relative).resolve()
        if project_root not in target_path.parents:
            raise PipelineError(
                f"test path escapes the work directory: {candidate.test_file!r}"
            )
        if target_path.exists():
            raise PipelineError(
                f"test file already exists in the project: {normalized}"
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(candidate.source, encoding="utf-8")
        tests.append(
            MaterializedTest(
                test_id=candidate.test_id,
                target_symbol=candidate.target_symbol,
                test_file=normalized,
                test_name=candidate.test_name,
                path=str(target_path),
                source_digest=hashlib.sha256(
                    candidate.source.encode("utf-8")
                ).hexdigest()[:16],
            )
        )

    return MaterializedProject(
        project_root=str(project_root),
        artifacts_root=str(artifacts),
        tests=tests,
    )
