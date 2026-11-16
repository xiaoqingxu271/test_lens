from __future__ import annotations

import ast
from pathlib import Path

from sandbox.runner import ProjectProfile, SandboxConfig, TargetSymbol

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "sample_project"


def make_profile(test_file: str) -> ProjectProfile:
    source = (FIXTURE_ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "classify"
    )
    target = TargetSymbol(
        symbol_id="classify",
        name="classify",
        file=FIXTURE_ROOT / "app.py",
        line_start=function.lineno,
        line_end=function.end_lineno or function.lineno,
    )
    return ProjectProfile(
        profile_id="sample-project",
        root=FIXTURE_ROOT,
        test_paths=[FIXTURE_ROOT / test_file],
        target_symbols=[target],
    )


def sandbox_config(**overrides) -> SandboxConfig:
    base = SandboxConfig(timeout_seconds=30.0, double_run=False)
    return base.model_copy(update=overrides)
