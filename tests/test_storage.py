from __future__ import annotations

import json

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
from evaluation.models import (
    AuditSideEvaluation,
    EvaluationReport,
    EvaluationTrace,
    FixtureEvaluation,
    TestSideEvaluation as SideEvaluation,
)
from llm.models import ModelKind
from pipeline.models import (
    DiscardReason,
    DiscardedTest,
    KeptTest,
    TestPipelineTrace as PipelineTrace,
    TestReport as PipelineReport,
)
from sandbox.runner import EnforcementMode, FinalStatus
from storage.db import (
    open_database,
    save_audit_report,
    save_evaluation_report,
    save_test_report,
)

TIMESTAMP = "2026-01-01T00:00:00+00:00"


def _test_report() -> PipelineReport:
    return PipelineReport(
        kept=[
            KeptTest(
                target_symbol="app.py::classify",
                test_file="generated/test_classify.py",
                test_name="test_classify",
                mutation_kill_rate=1.0,
                mutations_run=4,
                mutations_killed=4,
            )
        ],
        discarded=[
            DiscardedTest(
                test_id="generated/test_weak.py::test_weak",
                target_symbol="app.py::classify",
                test_file="generated/test_weak.py",
                test_name="test_weak",
                reason=DiscardReason.ASSERTION_TOO_WEAK,
                detail="kill rate 0.00 is below threshold 0.50",
                final_status=FinalStatus.PASSED,
                mutation_kill_rate=0.0,
                mutations_run=4,
                mutations_killed=0,
            )
        ],
        flaky=[],
        trace=PipelineTrace(
            started_at=TIMESTAMP,
            elapsed_ms=10,
            candidates_in=2,
            kept=1,
            discarded_by_reason={DiscardReason.ASSERTION_TOO_WEAK: 1},
            double_run=True,
            kill_threshold=0.5,
            seed=0,
            platform="Linux",
            enforcement_mode=EnforcementMode.ENFORCED,
        ),
    )


def _finding(
    finding_id: str,
    rule_id: RuleId,
    line: int,
    level: EvidenceLevel = EvidenceLevel.STATIC_CONFIRMED,
) -> Finding:
    return Finding(
        finding_id=finding_id,
        file="app.py",
        line_start=line,
        line_end=line,
        rule_id=rule_id,
        source=FindingSource.RULE,
        title=rule_id.value,
        evidence_level=level,
    )


def _audit_report() -> AuditReport:
    return AuditReport(
        findings=[
            _finding("f1", RuleId.R001_MUTABLE_DEFAULT, 10),
            _finding(
                "f2",
                RuleId.R002_NONE_DEREF_NO_GUARD,
                20,
                EvidenceLevel.REPRODUCED,
            ),
        ],
        retracted=[
            Retracted(
                finding=_finding(
                    "f3",
                    RuleId.R005_RESOURCE_NO_WITH,
                    30,
                    EvidenceLevel.SUSPICIOUS,
                ),
                reason=RetractReason.REFUTED_BY_CONTEXT,
            )
        ],
        trace=GradingTrace(
            started_at=TIMESTAMP,
            elapsed_ms=5,
            candidates_considered=3,
            findings_emitted=2,
            retracted=1,
            evidence_level_counts={
                EvidenceLevel.STATIC_CONFIRMED: 1,
                EvidenceLevel.REPRODUCED: 1,
            },
            retract_reason_counts={RetractReason.REFUTED_BY_CONTEXT: 1},
        ),
    )


def test_database_initialization_is_idempotent(tmp_path):
    database = tmp_path / "metadata.sqlite"

    with open_database(database) as conn:
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()[0]
        assert version == "1"

    with open_database(database) as conn:
        rows = conn.execute("SELECT value FROM schema_meta").fetchall()
        assert len(rows) == 1


def test_test_and_audit_reports_are_projected_into_queryable_rows(tmp_path):
    database = tmp_path / "metadata.sqlite"

    with open_database(database) as conn:
        test_run = save_test_report(
            conn,
            "project",
            _test_report(),
            model_kind="fake",
            model_id="fake:deterministic-v1",
        )
        audit_run = save_audit_report(
            conn,
            "project",
            _audit_report(),
            platform="Linux",
            enforcement_mode="enforced",
            double_run=True,
            model_kind="fake",
            model_id="fake:deterministic-v1",
        )

        run = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (test_run,)
        ).fetchone()
        assert run["kind"] == "tests"
        assert run["kept_count"] == 1
        assert run["discarded_count"] == 1
        assert run["threshold"] == 0.5
        assert run["double_run"] == 1
        payload = json.loads(run["payload_json"])
        assert payload["kept"][0]["test_name"] == "test_classify"

        kept = conn.execute(
            "SELECT * FROM kept_tests WHERE run_id = ?", (test_run,)
        ).fetchall()
        assert len(kept) == 1
        assert kept[0]["mutation_kill_rate"] == 1.0

        discarded = conn.execute(
            "SELECT * FROM discarded_tests WHERE run_id = ?", (test_run,)
        ).fetchall()
        assert discarded[0]["reason"] == "ASSERTION_TOO_WEAK"
        assert discarded[0]["final_status"] == "passed"

        findings = conn.execute(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY finding_id",
            (audit_run,),
        ).fetchall()
        assert [row["finding_id"] for row in findings] == ["f1", "f2"]
        assert findings[1]["evidence_level"] == "REPRODUCED"

        retracted = conn.execute(
            "SELECT * FROM retracted WHERE run_id = ?", (audit_run,)
        ).fetchall()
        assert retracted[0]["reason"] == "REFUTED_BY_CONTEXT"


def test_evaluation_report_is_projected_with_fixture_metrics(tmp_path):
    database = tmp_path / "metadata.sqlite"
    report = EvaluationReport(
        fixtures=[
            FixtureEvaluation(
                project="fixture",
                test=SideEvaluation(
                    candidates=2,
                    kept=1,
                    discarded_by_reason={"ASSERTION_TOO_WEAK": 1},
                    mutations_run=4,
                    mutations_killed=4,
                    killed_by_assertion=4,
                    double_run=True,
                    platform="Linux",
                    enforcement_mode=EnforcementMode.ENFORCED,
                    threshold=0.5,
                ),
                audit=AuditSideEvaluation(
                    findings_emitted=1,
                    precision=1.0,
                    recall=1.0,
                    evidence_level_counts={"REPRODUCED": 1},
                ),
            )
        ],
        calibration=[],
        trace=EvaluationTrace(
            started_at=TIMESTAMP,
            elapsed_ms=20,
            fixtures=["fixture"],
            model_kind=ModelKind.FAKE,
            model_id="fake:deterministic-v1",
            threshold=0.5,
            seed=0,
            double_run=True,
            platform="Linux",
            enforcement_mode=EnforcementMode.ENFORCED,
            sandbox_ready=True,
        ),
    )

    with open_database(database) as conn:
        run_id = save_evaluation_report(conn, report)
        run = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert run["kind"] == "eval"
        assert run["kept_count"] == 1
        assert run["finding_count"] == 1

        fixture = conn.execute(
            "SELECT * FROM evaluation_fixtures WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert fixture["project"] == "fixture"
        assert fixture["precision"] == 1.0
        assert json.loads(fixture["discarded_json"]) == {
            "ASSERTION_TOO_WEAK": 1
        }
