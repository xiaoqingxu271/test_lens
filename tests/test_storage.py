from __future__ import annotations

import json
import sqlite3

from audit.models import (
    AuditReport,
    ClaimKind,
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
        assert version == "2"

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


def _independent_finding(finding_id: str, line: int) -> Finding:
    return Finding(
        finding_id=finding_id,
        file="app.py",
        line_start=line,
        line_end=line,
        rule_id=None,
        llm_finding_id=None,
        source=FindingSource.LLM_INDEPENDENT,
        title="MISSING_INPUT_VALIDATION: classify",
        evidence_level=EvidenceLevel.SUSPICIOUS,
        claim="unvalidated input",
        claim_kind=ClaimKind.MISSING_INPUT_VALIDATION,
    )


def test_independent_findings_project_their_claim_kind(tmp_path):
    database = tmp_path / "metadata.sqlite"
    report = AuditReport(
        findings=[
            _independent_finding("i1", 5).model_copy(
                update={"evidence_level": EvidenceLevel.STATIC_CONFIRMED}
            )
        ],
        retracted=[
            Retracted(
                finding=_independent_finding("i2", 6),
                reason=RetractReason.UNVERIFIABLE_CLAIM,
            )
        ],
        trace=GradingTrace(
            started_at=TIMESTAMP,
            elapsed_ms=0,
            findings_emitted=1,
            retracted=1,
            independent_candidates_considered=2,
            independent_findings_emitted=1,
            independent_retracted=1,
        ),
    )

    with open_database(database) as conn:
        run_id = save_audit_report(conn, "project", report)
        finding = conn.execute(
            "SELECT * FROM findings WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert finding["claim_kind"] == "MISSING_INPUT_VALIDATION"
        assert finding["rule_id"] is None
        assert finding["source"] == "LLM_INDEPENDENT"

        retracted = conn.execute(
            "SELECT * FROM retracted WHERE run_id = ?", (run_id,)
        ).fetchone()
        assert retracted["claim_kind"] == "MISSING_INPUT_VALIDATION"
        assert retracted["reason"] == "UNVERIFIABLE_CLAIM"


def test_version_1_databases_gain_the_claim_kind_columns(tmp_path):
    database = tmp_path / "old.sqlite"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO schema_meta(key, value) VALUES ('version', '1');
        CREATE TABLE findings (
            run_id TEXT NOT NULL,
            finding_id TEXT NOT NULL,
            file TEXT NOT NULL,
            line_start INTEGER NOT NULL,
            line_end INTEGER NOT NULL,
            rule_id TEXT,
            llm_finding_id TEXT,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            evidence_level TEXT NOT NULL,
            PRIMARY KEY (run_id, finding_id)
        );
        CREATE TABLE retracted (
            run_id TEXT NOT NULL,
            finding_id TEXT NOT NULL,
            rule_id TEXT,
            file TEXT NOT NULL,
            line_start INTEGER NOT NULL,
            line_end INTEGER NOT NULL,
            evidence_level TEXT NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY (run_id, finding_id)
        );
        """
    )
    conn.commit()
    conn.close()

    with open_database(database) as conn:
        for table in ("findings", "retracted"):
            columns = {
                row[1]
                for row in conn.execute(f"PRAGMA table_info({table})")
            }
            assert "claim_kind" in columns
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()[0]
        assert version == "2"


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
