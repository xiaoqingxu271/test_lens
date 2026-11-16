"""Fixture label and proposal loading.

Labels identify expected findings by an exact source marker rather than a
hand-maintained line number, so a fixture edit cannot silently drift from its
ground truth. The marker must resolve to exactly one line during evaluation.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from .models import EvaluationLabels, Label


class FixtureError(ValueError):
    """The fixture contract is invalid or inconsistent."""


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"{path}: {type(exc).__name__}: {exc}") from exc


def _resolve_marker(root: Path, relative_file: str, marker: str) -> int:
    candidate = PurePosixPath(relative_file)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise FixtureError(f"label file escapes the fixture: {relative_file!r}")
    path = root / candidate
    if not path.is_file():
        raise FixtureError(f"label file does not exist: {relative_file}")
    lines = path.read_text(encoding="utf-8").splitlines()
    matches = [
        index + 1 for index, line in enumerate(lines) if marker in line
    ]
    if len(matches) != 1:
        raise FixtureError(
            f"{relative_file}: marker {marker!r} resolved to {len(matches)} lines"
        )
    return matches[0]


def _label(root: Path, raw: object) -> Label:
    if not isinstance(raw, dict):
        raise FixtureError("each label must be an object")
    relative_file = raw.get("file")
    rule_id = raw.get("rule_id")
    marker = raw.get("marker")
    if not all(isinstance(value, str) for value in (relative_file, rule_id, marker)):
        raise FixtureError("label requires string file, rule_id, and marker")
    return Label(
        file=relative_file,
        rule_id=rule_id,
        marker=marker,
        line=_resolve_marker(root, relative_file, marker),
        case=raw.get("case") if isinstance(raw.get("case"), str) else None,
        retract_reason=(
            raw.get("retract_reason")
            if isinstance(raw.get("retract_reason"), str)
            else None
        ),
    )


def load_labels(root: Path) -> EvaluationLabels:
    payload = _read_json(root / "labels.json")
    if not isinstance(payload, dict):
        raise FixtureError("labels.json must be an object")
    project = payload.get("project")
    if not isinstance(project, str) or not project:
        raise FixtureError("labels.json requires a project name")
    return EvaluationLabels(
        project=project,
        defects=[_label(root, item) for item in payload.get("defects", [])],
        false_positives=[
            _label(root, item) for item in payload.get("false_positives", [])
        ],
        false_negatives=[
            _label(root, item) for item in payload.get("false_negatives", [])
        ],
    )


def load_proposals(root: Path) -> dict[str, str]:
    payload = _read_json(root / "proposals.json")
    if not isinstance(payload, dict):
        raise FixtureError("proposals.json must be an object")
    responses: dict[str, str] = {}
    for request_id, value in payload.items():
        if not isinstance(request_id, str) or not isinstance(value, (dict, str)):
            raise FixtureError(
                "proposals.json must map request ids to objects or strings"
            )
        responses[request_id] = (
            value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        )
    return responses


def fixture_roots(root: Path) -> list[Path]:
    if not root.is_dir():
        raise FixtureError(f"fixture directory does not exist: {root}")
    projects = [
        path
        for path in sorted(root.iterdir())
        if path.is_dir()
        and (path / "app.py").is_file()
        and (path / "labels.json").is_file()
        and (path / "proposals.json").is_file()
    ]
    if not projects:
        raise FixtureError(f"no fixture projects found under {root}")
    return projects
