from __future__ import annotations

import json
from pathlib import Path

import cli.main as cli
import generation.generate as generation_module
from evaluation.models import (
    EvaluationReport,
    EvaluationTrace,
)
from helpers import sandbox_health
from sandbox.runner import EnforcementMode
from storage.db import open_database

AUDIT_ROOT = Path(__file__).parent / "fixtures" / "audit_project" / "review"
TIMESTAMP = "2026-01-01T00:00:00+00:00"


def _project(root: Path) -> Path:
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
""",
        encoding="utf-8",
    )
    return project


def _proposals(root: Path) -> Path:
    path = root / "proposals.json"
    path.write_text(
        json.dumps(
            {
                "test_generation::app.py::value": {
                    "tests": [
                        {
                            "test_file": "generated/test_value.py",
                            "test_name": "test_value",
                            "source": (
                                "from app import value\n\n\n"
                                "def test_value():\n"
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
    return path


def test_tests_cli_runs_pipeline_and_writes_database(
    tmp_path, monkeypatch, capsys
):
    project = _project(tmp_path)
    proposals = _proposals(tmp_path)
    database = tmp_path / "metadata.sqlite"
    monkeypatch.setattr(
        generation_module,
        "check_sandbox_ready",
        lambda **kwargs: sandbox_health(),
    )

    exit_code = cli.main(
        [
            "tests",
            str(project),
            "--proposals",
            str(proposals),
            "--json",
            "--db",
            str(database),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kept"][0]["test_name"] == "test_value"
    assert payload["trace"]["double_run"] is True
    assert payload["trace"]["kill_threshold"] == 0.5

    with open_database(database) as conn:
        run = conn.execute("SELECT * FROM runs").fetchone()
        assert run["kind"] == "tests"
        assert run["kept_count"] == 1
        kept = conn.execute("SELECT * FROM kept_tests").fetchall()
        assert kept[0]["test_name"] == "test_value"


def test_audit_cli_writes_findings_and_retractions(tmp_path, capsys):
    database = tmp_path / "metadata.sqlite"

    exit_code = cli.main(
        [
            "audit",
            str(AUDIT_ROOT),
            "--json",
            "--db",
            str(database),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["findings"]) == 3
    assert len(payload["retracted"]) == 2
    assert payload["trace"]["evidence_level_counts"] == {
        "REPRODUCED": 1,
        "STATIC_CONFIRMED": 2,
    }

    with open_database(database) as conn:
        run = conn.execute("SELECT * FROM runs").fetchone()
        assert run["kind"] == "audit"
        assert run["finding_count"] == 3
        assert run["retracted_count"] == 2
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM retracted").fetchone()[0] == 2


def _independent_project(root: Path) -> Path:
    project = root / "independent_project"
    project.mkdir(parents=True)
    (project / "app.py").write_text(
        """\
def value(flag):
    if flag:
        return 1
    return 0
""",
        encoding="utf-8",
    )
    return project


def _independent_proposals(root: Path) -> Path:
    path = root / "independent_proposals.json"
    path.write_text(
        json.dumps(
            {
                "llm_independent::app.py::value": {
                    "claims": [
                        {
                            "claim_kind": "MISSING_INPUT_VALIDATION",
                            "quote": "if flag:",
                            "claim": "flag is used without validation",
                            "trigger_path": ["flag", "return 1"],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_audit_cli_runs_independent_claims_and_reports_retractions(
    tmp_path, capsys
):
    project = _independent_project(tmp_path)
    proposals = _independent_proposals(tmp_path)

    exit_code = cli.main(
        [
            "audit",
            str(project),
            "--independent",
            "--proposals",
            str(proposals),
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"] == []
    assert len(payload["retracted"]) == 1
    retracted = payload["retracted"][0]
    assert retracted["reason"] == "UNVERIFIABLE_CLAIM"
    assert retracted["finding"]["source"] == "LLM_INDEPENDENT"
    assert retracted["finding"]["rule_id"] is None
    assert retracted["finding"]["claim_kind"] == "MISSING_INPUT_VALIDATION"
    assert payload["trace"]["independent_candidates_considered"] == 1
    assert payload["trace"]["independent_retracted"] == 1


def test_audit_cli_skips_independent_claims_without_the_flag(tmp_path, capsys):
    project = _independent_project(tmp_path)
    proposals = _independent_proposals(tmp_path)

    exit_code = cli.main(
        ["audit", str(project), "--proposals", str(proposals), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["retracted"] == []
    assert payload["trace"]["independent_candidates_considered"] == 0


def test_tests_cli_returns_3_when_the_sandbox_is_unavailable(
    tmp_path, monkeypatch, capsys
):
    project = _project(tmp_path)
    monkeypatch.setattr(
        generation_module,
        "check_sandbox_ready",
        lambda **kwargs: sandbox_health(ready=False),
    )

    exit_code = cli.main(["tests", str(project)])

    assert exit_code == 3
    assert "sandbox unavailable" in capsys.readouterr().err


def test_cli_rejects_a_missing_project(tmp_path, capsys):
    exit_code = cli.main(["audit", str(tmp_path / "missing")])

    assert exit_code == 2
    assert "does not exist" in capsys.readouterr().err


def test_eval_cli_delegates_to_the_harness(monkeypatch, capsys):
    captured: dict[str, object] = {}
    report = EvaluationReport(
        fixtures=[],
        calibration=[],
        trace=EvaluationTrace(
            started_at=TIMESTAMP,
            elapsed_ms=1,
            fixtures=[],
            model_id="fake:deterministic-v1",
            platform="Linux",
            enforcement_mode=EnforcementMode.ENFORCED,
            sandbox_ready=True,
        ),
    )

    def fake_run_evaluation(**kwargs):
        captured.update(kwargs)
        return report

    monkeypatch.setattr(cli, "run_evaluation", fake_run_evaluation)

    exit_code = cli.main(
        ["eval", "--threshold", "0.4", "--seed", "2", "--independent"]
    )

    assert exit_code == 0
    assert captured["threshold"] == 0.4
    assert captured["seed"] == 2
    assert captured["independent"] is True
    assert captured["write"] is True
    assert capsys.readouterr().out == ""


def test_cli_requires_an_image_for_the_container_backend(tmp_path, capsys):
    project = _project(tmp_path)
    exit_code = cli.main(
        ["tests", str(project), "--sandbox-backend", "container"]
    )

    assert exit_code == 2
    assert "--sandbox-image is required" in capsys.readouterr().err


def test_cli_rejects_an_image_without_the_container_backend(tmp_path, capsys):
    project = _project(tmp_path)
    exit_code = cli.main(["tests", str(project), "--sandbox-image", "image:1"])

    assert exit_code == 2
    assert "requires --sandbox-backend container" in capsys.readouterr().err


def _empty_transcript(tmp_path: Path) -> Path:
    path = tmp_path / "transcript.json"
    path.write_text(
        json.dumps(
            {
                "transcript_version": 1,
                "created_by": "testlens",
                "model_id": "recorded-model",
                "model_kind": "real",
                "recordings": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_cli_rejects_model_option_misuse(tmp_path, capsys):
    transcript = _empty_transcript(tmp_path)

    assert cli.main(["audit", str(AUDIT_ROOT), "--transcript", str(transcript)]) == 2

    assert cli.main(["audit", str(AUDIT_ROOT), "--llm", "replay"]) == 2

    assert cli.main(["audit", str(AUDIT_ROOT), "--llm", "real"]) == 2

    assert (
        cli.main(
            [
                "audit",
                str(AUDIT_ROOT),
                "--llm",
                "real",
                "--model-id",
                "model-x",
                "--transcript",
                str(transcript),
            ]
        )
        == 2
    )


def test_proposals_are_rejected_outside_fake_mode(tmp_path, capsys):
    project = _project(tmp_path)
    transcript = _empty_transcript(tmp_path)
    proposals = _proposals(tmp_path)

    exit_code = cli.main(
        [
            "tests",
            str(project),
            "--llm",
            "replay",
            "--transcript",
            str(transcript),
            "--proposals",
            str(proposals),
        ]
    )

    assert exit_code == 2
    assert "--proposals requires --llm fake" in capsys.readouterr().err


def test_replay_without_a_recording_exits_with_the_model_code(tmp_path, capsys):
    transcript = _empty_transcript(tmp_path)

    exit_code = cli.main(
        [
            "audit",
            str(AUDIT_ROOT),
            "--llm",
            "replay",
            "--transcript",
            str(transcript),
        ]
    )

    assert exit_code == 4
    assert "no recorded response" in capsys.readouterr().err


def test_missing_credentials_exit_with_the_model_code(monkeypatch, capsys):
    monkeypatch.delenv("TESTLENS_LLM_API_KEY", raising=False)

    exit_code = cli.main(
        ["audit", str(AUDIT_ROOT), "--llm", "real", "--model-id", "model-x"]
    )

    assert exit_code == 4
    assert "TESTLENS_LLM_API_KEY" in capsys.readouterr().err


def test_eval_cli_reports_usage_errors_for_model_options(capsys):
    exit_code = cli.main(["eval", "--llm", "replay"])

    assert exit_code == 2
    assert "--transcript" in capsys.readouterr().err
