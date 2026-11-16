from __future__ import annotations

import json
from pathlib import Path

from audit.models import (
    AuditReport,
    EvidenceLevel,
    Finding,
    FindingSource,
    GradingTrace,
    Retracted,
    RetractReason,
    RuleId,
)
from evaluation.labels import fixture_roots, load_labels, load_proposals
from evaluation.metrics import calibration_rows, summarize_audit_side
from evaluation.models import (
    EvaluationLabels,
    Label,
    TestSideEvaluation as SideEvaluation,
)
from evaluation.run import DEFAULT_FIXTURES, run_evaluation
from generation.generate import generation_request_id
from helpers import make_root_profile
from targets.selection import select_targets

TIMESTAMP = "2026-01-01T00:00:00+00:00"
ALL_RULES = {
    RuleId.R001_MUTABLE_DEFAULT.value,
    RuleId.R002_NONE_DEREF_NO_GUARD.value,
    RuleId.R003_UNREACHABLE_AFTER_RETURN.value,
    RuleId.R004_BARE_EXCEPT_SWALLOW.value,
    RuleId.R005_RESOURCE_NO_WITH.value,
}


def test_fixture_contracts_cover_all_rules_and_targets():
    roots = fixture_roots(DEFAULT_FIXTURES)
    assert [root.name for root in roots] == [
        "fastapi_project",
        "flask_project",
    ]

    for root in roots:
        labels = load_labels(root)
        assert {label.rule_id for label in labels.defects} == ALL_RULES
        assert len(labels.false_positives) == 2
        assert len(labels.false_negatives) == 1
        assert all(label.line > 0 for label in labels.defects)

        proposals = load_proposals(root)
        selection = select_targets(make_root_profile(root))
        assert set(proposals) == {
            generation_request_id(target.symbol_id)
            for target in selection.targets
        }


def test_calibration_thresholds_recompute_from_measured_kill_rates():
    summary = SideEvaluation(
        mutation_kill_rates=[0.0, 0.5, 1.0],
        unscored_discarded_by_reason={"FLAKY": 1},
    )

    rows = calibration_rows("project", summary, [0.3, 0.5, 0.7])

    assert [(row.threshold, row.kept) for row in rows] == [
        (0.3, 2),
        (0.5, 2),
        (0.7, 1),
    ]
    assert rows[0].discarded_by_reason == {
        "ASSERTION_TOO_WEAK": 1,
        "FLAKY": 1,
    }
    assert rows[2].discarded_by_reason == {
        "ASSERTION_TOO_WEAK": 2,
        "FLAKY": 1,
    }


def _finding(
    line: int,
    rule_id: RuleId,
    *,
    file: str = "app.py",
    level: EvidenceLevel = EvidenceLevel.STATIC_CONFIRMED,
) -> Finding:
    return Finding(
        finding_id=f"f{line}",
        file=file,
        line_start=line,
        line_end=line,
        rule_id=rule_id,
        source=FindingSource.RULE,
        title=rule_id.value,
        evidence_level=level,
    )


def test_audit_metrics_count_true_positives_false_positives_and_retractions():
    labels = EvaluationLabels(
        project="project",
        defects=[
            Label(
                file="app.py",
                rule_id=RuleId.R001_MUTABLE_DEFAULT.value,
                marker="def f(items=[]):",
                line=10,
            )
        ],
        false_positives=[
            Label(
                file="app.py",
                rule_id=RuleId.R005_RESOURCE_NO_WITH.value,
                marker="finally_handle = open(",
                line=20,
                retract_reason=RetractReason.REFUTED_BY_CONTEXT.value,
            )
        ],
        false_negatives=[
            Label(
                file="app.py",
                rule_id=RuleId.R005_RESOURCE_NO_WITH.value,
                marker="with sqlite3.connect(",
                line=30,
            )
        ],
    )
    report = AuditReport(
        findings=[
            _finding(10, RuleId.R001_MUTABLE_DEFAULT),
            _finding(30, RuleId.R005_RESOURCE_NO_WITH),
        ],
        retracted=[
            Retracted(
                finding=_finding(
                    20,
                    RuleId.R005_RESOURCE_NO_WITH,
                    level=EvidenceLevel.SUSPICIOUS,
                ),
                reason=RetractReason.REFUTED_BY_CONTEXT,
            )
        ],
        trace=GradingTrace(
            started_at=TIMESTAMP,
            elapsed_ms=0,
            candidates_considered=3,
            findings_emitted=2,
            retracted=1,
            evidence_level_counts={EvidenceLevel.STATIC_CONFIRMED: 2},
            retract_reason_counts={RetractReason.REFUTED_BY_CONTEXT: 1},
        ),
    )

    summary = summarize_audit_side(report, labels)

    assert summary.candidates == 3
    assert summary.findings_emitted == 2
    assert summary.true_positives == 1
    assert summary.false_positives == 1
    assert summary.precision == 0.5
    assert summary.defects_found == 1
    assert summary.recall == 1.0
    assert summary.false_positive_labels == 1
    assert summary.false_positives_retracted == 1
    assert summary.false_negative_labels == 1
    assert summary.false_negatives_observed == 1
    by_rule = {item.rule_id: item for item in summary.per_rule}
    assert by_rule[RuleId.R001_MUTABLE_DEFAULT.value].true_positives == 1
    assert by_rule[RuleId.R005_RESOURCE_NO_WITH.value].false_positives == 1


def test_evaluation_runs_both_fixtures():
    report = run_evaluation(write=False)

    assert [fixture.project for fixture in report.fixtures] == [
        "fastapi_project",
        "flask_project",
    ]

    for fixture in report.fixtures:
        test = fixture.test
        assert test.candidates == 5
        assert test.kept == 1
        assert test.discarded_by_reason == {
            "ASSERTION_TOO_WEAK": 2,
            "FLAKY": 1,
            "RUNTIME_ERROR_ON_HAPPY": 1,
        }
        assert test.flaky == 1
        assert test.flaky_rate == 0.2
        assert test.mean_kill_rate == 1.0
        assert test.mutations_run > 0
        assert test.mutations_killed < test.mutations_run
        assert test.killed_by_assertion == test.mutations_killed
        assert test.killed_by_exception == 0
        assert test.killed_by_timeout == 0
        assert test.mean_kill_rate_scored is not None
        assert 0.0 < test.mean_kill_rate_scored < 1.0
        assert test.double_run is True
        assert test.platform
        assert test.enforcement_mode.value in {"enforced", "best-effort"}
        assert test.seed == 0
        assert test.threshold == 0.5
        assert test.operator_set_version == "v1"
        assert test.equivalent_mutants_undetected is True

        audit = fixture.audit
        assert audit.findings_emitted == 5
        assert audit.defects_labeled == 5
        assert audit.defects_found == 5
        assert audit.precision == 1.0
        assert audit.recall == 1.0
        assert audit.false_positives == 0
        assert audit.false_positive_labels == 2
        assert audit.false_positives_retracted == 2
        assert audit.false_negative_labels == 1
        assert audit.false_negatives_observed == 0
        assert audit.evidence_level_counts == {
            "REPRODUCED": 1,
            "STATIC_CONFIRMED": 4,
        }
        assert audit.retract_reason_counts == {"REFUTED_BY_CONTEXT": 2}
        assert len(audit.per_rule) == 5

    trace = report.trace
    assert trace.double_run is True
    assert trace.model_kind.value == "fake"
    assert trace.model_id
    assert trace.sandbox_ready is True
    assert trace.seed == 0
    assert trace.threshold == 0.5
    assert trace.calibration_thresholds == [0.2, 0.5, 0.8]

    assert len(report.calibration) == 6
    kept_by_threshold = {0.2: 2, 0.5: 1, 0.8: 1}
    for row in report.calibration:
        assert row.kept == kept_by_threshold[row.threshold]
        assert row.discarded_by_reason["FLAKY"] == 1
        assert row.discarded_by_reason["RUNTIME_ERROR_ON_HAPPY"] == 1
        assert row.discarded_by_reason["ASSERTION_TOO_WEAK"] == (
            1 if row.threshold == 0.2 else 2
        )


def _tiny_fixture(root: Path) -> Path:
    project = root / "tiny_project"
    project.mkdir(parents=True)
    (project / "app.py").write_text(
        """\
class Stub:
    def get(self, path, **kwargs):
        return lambda function: function


app = Stub()


@app.get("/value")
def value(flag):
    if flag:
        return 1
    return 0


def collect(items=[]):
    return items
""",
        encoding="utf-8",
    )
    (project / "labels.json").write_text(
        json.dumps(
            {
                "project": "tiny_project",
                "defects": [
                    {
                        "file": "app.py",
                        "rule_id": "R001-MUTABLE-DEFAULT",
                        "marker": "def collect(items=[]):",
                    }
                ],
                "false_positives": [],
                "false_negatives": [],
            }
        ),
        encoding="utf-8",
    )
    (project / "proposals.json").write_text(
        json.dumps(
            {
                "test_generation::app.py::value": {
                    "tests": [
                        {
                            "test_file": "generated/test_value_strong.py",
                            "test_name": "test_value_strong",
                            "source": (
                                "from app import value\n\n\n"
                                "def test_value_strong():\n"
                                "    assert value(True) == 1\n"
                                "    assert value(False) == 0\n"
                            ),
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return project


def _stable(report):
    data = report.model_dump(mode="json")
    data.pop("output_dir", None)
    data["trace"].pop("started_at", None)
    data["trace"].pop("elapsed_ms", None)
    return data


def test_evaluation_writes_reports_and_is_deterministic(tmp_path):
    project = _tiny_fixture(tmp_path)
    output_root = tmp_path / "reports"

    first = run_evaluation(
        [project], output_root=output_root, write=True
    )
    second = run_evaluation(
        [project], output_root=output_root, write=True
    )

    assert first.output_dir is not None
    output_dir = Path(first.output_dir)
    assert output_dir.is_dir()
    for name in ("report.json", "calibration.json", "summary.md"):
        assert (output_dir / name).is_file()

    payload = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["trace"]["double_run"] is True
    assert payload["fixtures"][0]["project"] == "tiny_project"
    assert payload["fixtures"][0]["test"]["kept"] == 1
    assert payload["fixtures"][0]["audit"]["precision"] == 1.0
    assert "TestLens evaluation" in (output_dir / "summary.md").read_text(
        encoding="utf-8"
    )

    assert _stable(first) == _stable(second)
    assert first.output_dir != second.output_dir
