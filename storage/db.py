"""SQLite metadata for test, audit, and evaluation reports.

Stage reports stay Pydantic objects; this module only projects them into
queryable rows and keeps the complete JSON payload alongside the normalized
columns so no evidence is lost.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from audit.models import AuditReport
from evaluation.models import EvaluationReport
from pipeline.models import TestReport

SCHEMA_VERSION = "2"

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('tests', 'audit', 'eval')),
    project TEXT NOT NULL,
    started_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    enforcement_mode TEXT NOT NULL DEFAULT '',
    double_run INTEGER NOT NULL DEFAULT 1,
    threshold REAL,
    seed INTEGER,
    model_kind TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL DEFAULT '',
    kept_count INTEGER NOT NULL DEFAULT 0,
    discarded_count INTEGER NOT NULL DEFAULT 0,
    flaky_count INTEGER NOT NULL DEFAULT 0,
    finding_count INTEGER NOT NULL DEFAULT 0,
    retracted_count INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kept_tests (
    run_id TEXT NOT NULL,
    target_symbol TEXT NOT NULL,
    test_file TEXT NOT NULL,
    test_name TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    mutation_kill_rate REAL NOT NULL,
    mutations_run INTEGER NOT NULL,
    mutations_killed INTEGER NOT NULL,
    PRIMARY KEY (run_id, test_file, test_name),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS discarded_tests (
    run_id TEXT NOT NULL,
    test_id TEXT NOT NULL,
    target_symbol TEXT NOT NULL,
    test_file TEXT NOT NULL,
    test_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    final_status TEXT,
    mutation_kill_rate REAL,
    mutations_run INTEGER,
    mutations_killed INTEGER,
    PRIMARY KEY (run_id, test_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS findings (
    run_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    file TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    rule_id TEXT,
    llm_finding_id TEXT,
    source TEXT NOT NULL,
    claim_kind TEXT,
    title TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    PRIMARY KEY (run_id, finding_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS retracted (
    run_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    rule_id TEXT,
    file TEXT NOT NULL,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    evidence_level TEXT NOT NULL,
    claim_kind TEXT,
    reason TEXT NOT NULL,
    PRIMARY KEY (run_id, finding_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS evaluation_fixtures (
    run_id TEXT NOT NULL,
    project TEXT NOT NULL,
    candidates INTEGER NOT NULL,
    kept INTEGER NOT NULL,
    flaky INTEGER NOT NULL,
    flaky_rate REAL NOT NULL,
    mean_kill_rate REAL,
    mutations_run INTEGER NOT NULL,
    mutations_killed INTEGER NOT NULL,
    killed_by_assertion INTEGER NOT NULL,
    killed_by_exception INTEGER NOT NULL,
    killed_by_timeout INTEGER NOT NULL,
    findings_emitted INTEGER NOT NULL,
    precision REAL,
    recall REAL,
    false_positives INTEGER NOT NULL,
    false_positives_retracted INTEGER NOT NULL,
    false_negatives_observed INTEGER NOT NULL,
    discarded_json TEXT NOT NULL,
    evidence_levels_json TEXT NOT NULL,
    retract_reasons_json TEXT NOT NULL,
    PRIMARY KEY (run_id, project),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_kind ON runs(kind, started_at);
CREATE INDEX IF NOT EXISTS idx_findings_rule ON findings(run_id, rule_id);
"""


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "findings", "claim_kind", "TEXT")
    _add_column_if_missing(conn, "retracted", "claim_kind", "TEXT")


def initialize(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'version'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('version', ?)",
            (SCHEMA_VERSION,),
        )
    elif row[0] == "1":
        _migrate_v1_to_v2(conn)
        conn.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'version'",
            (SCHEMA_VERSION,),
        )
    elif row[0] != SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported database schema version {row[0]!r}; "
            f"expected {SCHEMA_VERSION!r}"
        )
    conn.commit()


@contextmanager
def open_database(path: Path | str) -> Iterator[sqlite3.Connection]:
    database = Path(path).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        initialize(conn)
        yield conn
    finally:
        conn.close()


def _payload(model) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True)


def _insert_run(
    conn: sqlite3.Connection,
    *,
    kind: str,
    project: str,
    started_at: str,
    platform: str,
    enforcement_mode: str,
    double_run: bool,
    threshold: float | None,
    seed: int | None,
    model_kind: str,
    model_id: str,
    kept_count: int,
    discarded_count: int,
    flaky_count: int,
    finding_count: int,
    retracted_count: int,
    payload_json: str,
) -> str:
    initialize(conn)
    run_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO runs(
            run_id, kind, project, started_at, created_at, platform,
            enforcement_mode, double_run, threshold, seed, model_kind,
            model_id, kept_count, discarded_count, flaky_count,
            finding_count, retracted_count, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            kind,
            project,
            started_at,
            datetime.now(timezone.utc).isoformat(),
            platform,
            enforcement_mode,
            1 if double_run else 0,
            threshold,
            seed,
            model_kind,
            model_id,
            kept_count,
            discarded_count,
            flaky_count,
            finding_count,
            retracted_count,
            payload_json,
        ),
    )
    return run_id


def save_test_report(
    conn: sqlite3.Connection,
    project: str,
    report: TestReport,
    *,
    model_kind: str = "",
    model_id: str = "",
) -> str:
    trace = report.trace
    run_id = _insert_run(
        conn,
        kind="tests",
        project=project,
        started_at=trace.started_at,
        platform=trace.platform,
        enforcement_mode=trace.enforcement_mode.value,
        double_run=trace.double_run,
        threshold=trace.kill_threshold,
        seed=trace.seed,
        model_kind=model_kind,
        model_id=model_id,
        kept_count=len(report.kept),
        discarded_count=len(report.discarded),
        flaky_count=len(report.flaky),
        finding_count=0,
        retracted_count=0,
        payload_json=_payload(report),
    )
    conn.executemany(
        """
        INSERT INTO kept_tests(
            run_id, target_symbol, test_file, test_name, execution_status,
            mutation_kill_rate, mutations_run, mutations_killed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item.target_symbol,
                item.test_file,
                item.test_name,
                item.execution_status,
                item.mutation_kill_rate,
                item.mutations_run,
                item.mutations_killed,
            )
            for item in report.kept
        ],
    )
    conn.executemany(
        """
        INSERT INTO discarded_tests(
            run_id, test_id, target_symbol, test_file, test_name, reason,
            detail, final_status, mutation_kill_rate, mutations_run,
            mutations_killed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item.test_id,
                item.target_symbol,
                item.test_file,
                item.test_name,
                item.reason.value,
                item.detail,
                item.final_status.value if item.final_status is not None else None,
                item.mutation_kill_rate,
                item.mutations_run,
                item.mutations_killed,
            )
            for item in report.discarded
        ],
    )
    conn.commit()
    return run_id


def save_audit_report(
    conn: sqlite3.Connection,
    project: str,
    report: AuditReport,
    *,
    platform: str = "",
    enforcement_mode: str = "",
    double_run: bool = True,
    model_kind: str = "",
    model_id: str = "",
) -> str:
    trace = report.trace
    run_id = _insert_run(
        conn,
        kind="audit",
        project=project,
        started_at=trace.started_at,
        platform=platform,
        enforcement_mode=enforcement_mode,
        double_run=double_run,
        threshold=None,
        seed=None,
        model_kind=model_kind,
        model_id=model_id,
        kept_count=0,
        discarded_count=0,
        flaky_count=0,
        finding_count=len(report.findings),
        retracted_count=len(report.retracted),
        payload_json=_payload(report),
    )
    conn.executemany(
        """
        INSERT INTO findings(
            run_id, finding_id, file, line_start, line_end, rule_id,
            llm_finding_id, source, claim_kind, title, evidence_level
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item.finding_id,
                item.file,
                item.line_start,
                item.line_end,
                item.rule_id.value if item.rule_id is not None else None,
                item.llm_finding_id,
                item.source.value,
                (
                    item.claim_kind.value
                    if item.claim_kind is not None
                    else None
                ),
                item.title,
                item.evidence_level.value,
            )
            for item in report.findings
        ],
    )
    conn.executemany(
        """
        INSERT INTO retracted(
            run_id, finding_id, rule_id, file, line_start, line_end,
            evidence_level, claim_kind, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                item.finding.finding_id,
                (
                    item.finding.rule_id.value
                    if item.finding.rule_id is not None
                    else None
                ),
                item.finding.file,
                item.finding.line_start,
                item.finding.line_end,
                item.finding.evidence_level.value,
                (
                    item.finding.claim_kind.value
                    if item.finding.claim_kind is not None
                    else None
                ),
                item.reason.value,
            )
            for item in report.retracted
        ],
    )
    conn.commit()
    return run_id


def save_evaluation_report(
    conn: sqlite3.Connection, report: EvaluationReport
) -> str:
    trace = report.trace
    discarded = sum(
        sum(fixture.test.discarded_by_reason.values())
        for fixture in report.fixtures
    )
    retracted = sum(
        sum(fixture.audit.retract_reason_counts.values())
        for fixture in report.fixtures
    )
    run_id = _insert_run(
        conn,
        kind="eval",
        project=",".join(trace.fixtures),
        started_at=trace.started_at,
        platform=trace.platform,
        enforcement_mode=trace.enforcement_mode.value,
        double_run=trace.double_run,
        threshold=trace.threshold,
        seed=trace.seed,
        model_kind=trace.model_kind.value,
        model_id=trace.model_id,
        kept_count=sum(fixture.test.kept for fixture in report.fixtures),
        discarded_count=discarded,
        flaky_count=sum(fixture.test.flaky for fixture in report.fixtures),
        finding_count=sum(
            fixture.audit.findings_emitted for fixture in report.fixtures
        ),
        retracted_count=retracted,
        payload_json=_payload(report),
    )
    conn.executemany(
        """
        INSERT INTO evaluation_fixtures(
            run_id, project, candidates, kept, flaky, flaky_rate,
            mean_kill_rate, mutations_run, mutations_killed,
            killed_by_assertion, killed_by_exception, killed_by_timeout,
            findings_emitted, precision, recall, false_positives,
            false_positives_retracted, false_negatives_observed,
            discarded_json, evidence_levels_json, retract_reasons_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                fixture.project,
                fixture.test.candidates,
                fixture.test.kept,
                fixture.test.flaky,
                fixture.test.flaky_rate,
                fixture.test.mean_kill_rate,
                fixture.test.mutations_run,
                fixture.test.mutations_killed,
                fixture.test.killed_by_assertion,
                fixture.test.killed_by_exception,
                fixture.test.killed_by_timeout,
                fixture.audit.findings_emitted,
                fixture.audit.precision,
                fixture.audit.recall,
                fixture.audit.false_positives,
                fixture.audit.false_positives_retracted,
                fixture.audit.false_negatives_observed,
                json.dumps(
                    fixture.test.discarded_by_reason, sort_keys=True
                ),
                json.dumps(
                    fixture.audit.evidence_level_counts, sort_keys=True
                ),
                json.dumps(
                    fixture.audit.retract_reason_counts, sort_keys=True
                ),
            )
            for fixture in report.fixtures
        ],
    )
    conn.commit()
    return run_id
