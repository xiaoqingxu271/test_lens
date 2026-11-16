"""Deterministic rule scan stage."""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from project.files import load_modules
from sandbox.runner import ProjectProfile

from .models import RuleId, RuleScanReport, RuleScanTrace
from .rules import ALL_RULES, scan_tree


def scan_rules(
    profile: ProjectProfile,
    *,
    rules: Sequence[RuleId] = ALL_RULES,
) -> RuleScanReport:
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()

    requested = set(rules)
    unknown = requested - set(ALL_RULES)
    if unknown:
        names = ", ".join(sorted(rule.value for rule in unknown))
        raise ValueError(f"rules not implemented: {names}")
    selected = tuple(rule for rule in ALL_RULES if rule in requested)

    root = Path(profile.root).expanduser().resolve()
    modules, skipped_files = load_modules(
        root,
        excluded_paths=profile.test_paths,
    )

    candidates = []
    for module in modules:
        candidates.extend(scan_tree(module, selected))
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

    by_rule = {rule: 0 for rule in selected}
    for candidate in candidates:
        by_rule[candidate.rule_id] += 1

    trace = RuleScanTrace(
        started_at=started_at,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        files_scanned=len(modules),
        files_skipped=skipped_files,
        candidates_found=len(candidates),
        candidates_by_rule=by_rule,
        rules_run=list(selected),
        warnings=[],
    )
    return RuleScanReport(candidates=candidates, trace=trace)
