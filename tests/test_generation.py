from __future__ import annotations

import json
from pathlib import Path

import pytest

from generation.generate import (
    GENERATION_TEMPLATE_VERSION,
    SandboxUnavailableError,
    generate_tests,
    generation_request_id,
)
from generation.models import GenerationRejectReason
from helpers import make_root_profile, sandbox_health
from llm.fake import DeterministicFakeLLM
from llm.models import ModelKind
from sandbox.health import SandboxHealthFailure
from targets.selection import select_targets


def _project(tmp_path: Path, names: list[str]) -> Path:
    lines = [
        "class Stub:",
        "    def get(self, path, **kwargs):",
        "        return lambda function: function",
        "",
        "app = Stub()",
        "",
    ]
    for name in names:
        lines.extend([f'@app.get("/{name}")', f"def {name}():", "    return {}", "", ""])
    (tmp_path / "app.py").write_text("\n".join(lines), encoding="utf-8")
    return tmp_path


def _response(test_file: str, test_name: str, source: str) -> str:
    return json.dumps(
        {
            "tests": [
                {"test_file": test_file, "test_name": test_name, "source": source}
            ]
        }
    )


def _valid_source(name: str, test_name: str) -> str:
    return f"from app import {name}\n\n\ndef {test_name}():\n    {name}()\n"


def _stable(report):
    data = report.model_dump(mode="json")
    data["trace"].pop("started_at", None)
    data["trace"].pop("elapsed_ms", None)
    return data


def _snapshot(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if "__pycache__" not in path.parts
    }


def test_generate_tests_accepts_default_fake_proposals(tmp_path):
    root = _project(tmp_path, ["read_item", "status"])
    profile = make_root_profile(root)
    selection = select_targets(profile)
    fake = DeterministicFakeLLM()

    report = generate_tests(profile, selection, fake, sandbox_health=sandbox_health())

    assert [candidate.test_id for candidate in report.candidates] == [
        "generated/test_app_read_item.py::test_read_item_smoke",
        "generated/test_app_status.py::test_status_smoke",
    ]
    assert [candidate.target_symbol for candidate in report.candidates] == [
        "app.py::read_item",
        "app.py::status",
    ]
    assert all(candidate.model_kind == ModelKind.FAKE for candidate in report.candidates)
    assert all(
        candidate.prompt_template_version == GENERATION_TEMPLATE_VERSION
        for candidate in report.candidates
    )
    assert all(candidate.prompt_digest and candidate.response_digest for candidate in report.candidates)
    assert "from app import read_item" in report.candidates[0].source
    assert report.candidates[0].source.count("def test_") == 1
    assert report.rejected == []

    trace = report.trace
    assert trace.targets_considered == 2
    assert trace.tests_proposed == 2
    assert trace.tests_accepted == 2
    assert trace.tests_rejected == 0
    assert trace.rejected_by_reason == {}
    assert trace.sandbox_health.ready is True
    assert trace.llm_model_kind == ModelKind.FAKE
    assert trace.warnings == []

    assert [call.request_id for call in fake.calls] == [
        generation_request_id("app.py::read_item"),
        generation_request_id("app.py::status"),
    ]
    assert '"symbol_id": "app.py::read_item"' in fake.calls[0].prompt
    assert '"parameters"' in fake.calls[0].prompt


def test_generate_tests_is_deterministic(tmp_path):
    root = _project(tmp_path, ["read_item", "status"])
    profile = make_root_profile(root)
    selection = select_targets(profile)

    first = generate_tests(
        profile, selection, DeterministicFakeLLM(), sandbox_health=sandbox_health()
    )
    second = generate_tests(
        profile, selection, DeterministicFakeLLM(), sandbox_health=sandbox_health()
    )

    assert _stable(first) == _stable(second)


def test_generate_tests_rejects_each_invalid_proposal_with_a_reason(tmp_path):
    names = [
        "not_json",
        "empty",
        "none",
        "syntax",
        "wrong_name",
        "mismatch",
        "escape",
        "absolute",
        "existing",
        "dup_one",
        "dup_two",
        "bad_shape",
    ]
    root = _project(tmp_path, names)
    (root / "test_existing.py").write_text("def test_existing():\n    pass\n", encoding="utf-8")

    responses = {
        generation_request_id("app.py::not_json"): "not json",
        generation_request_id("app.py::empty"): "",
        generation_request_id("app.py::none"): json.dumps({"tests": []}),
        generation_request_id("app.py::syntax"): _response(
            "generated/test_syntax.py", "test_syntax", "def test_syntax(:\n    pass\n"
        ),
        generation_request_id("app.py::wrong_name"): _response(
            "generated/test_wrong_name.py",
            "test_wrong_name",
            "from app import wrong_name\n\n\ndef test_other():\n    wrong_name()\n",
        ),
        generation_request_id("app.py::mismatch"): _response(
            "generated/test_mismatch.py",
            "test_mismatch",
            "def test_mismatch():\n    other()\n",
        ),
        generation_request_id("app.py::escape"): _response(
            "../escape/test_escape.py",
            "test_escape",
            _valid_source("escape", "test_escape"),
        ),
        generation_request_id("app.py::absolute"): _response(
            "/tmp/test_absolute.py",
            "test_absolute",
            _valid_source("absolute", "test_absolute"),
        ),
        generation_request_id("app.py::existing"): _response(
            "test_existing.py",
            "test_existing",
            _valid_source("existing", "test_existing"),
        ),
        generation_request_id("app.py::dup_one"): _response(
            "generated/test_dup.py", "test_dup", _valid_source("dup_one", "test_dup")
        ),
        generation_request_id("app.py::dup_two"): _response(
            "generated/test_dup.py", "test_dup", _valid_source("dup_two", "test_dup")
        ),
        generation_request_id("app.py::bad_shape"): json.dumps({"tests": ["nope"]}),
    }

    profile = make_root_profile(root)
    selection = select_targets(profile)
    report = generate_tests(
        profile,
        selection,
        DeterministicFakeLLM(responses),
        sandbox_health=sandbox_health(),
    )

    assert [candidate.test_id for candidate in report.candidates] == [
        "generated/test_dup.py::test_dup"
    ]
    assert report.candidates[0].target_symbol == "app.py::dup_one"

    assert report.trace.tests_proposed == 9
    assert report.trace.tests_accepted == 1
    assert report.trace.tests_rejected == 11
    assert report.trace.rejected_by_reason == {
        GenerationRejectReason.EMPTY_RESPONSE: 1,
        GenerationRejectReason.NO_PROPOSAL: 1,
        GenerationRejectReason.INVALID_JSON: 1,
        GenerationRejectReason.INVALID_SHAPE: 1,
        GenerationRejectReason.INVALID_TEST_PATH: 2,
        GenerationRejectReason.FILE_EXISTS: 1,
        GenerationRejectReason.SYNTAX_ERROR: 1,
        GenerationRejectReason.NO_TEST_FUNCTION: 1,
        GenerationRejectReason.TARGET_MISMATCH: 1,
        GenerationRejectReason.DUPLICATE_TEST_ID: 1,
    }

    by_reason = {
        (item.target_symbol, item.reason): item for item in report.rejected
    }
    assert by_reason[("app.py::syntax", GenerationRejectReason.SYNTAX_ERROR)].detail
    assert (
        by_reason[("app.py::wrong_name", GenerationRejectReason.NO_TEST_FUNCTION)]
        .test_file
        == "generated/test_wrong_name.py"
    )
    duplicate = by_reason[("app.py::dup_two", GenerationRejectReason.DUPLICATE_TEST_ID)]
    assert "app.py::dup_one" in duplicate.detail
    assert duplicate.test_file == "generated/test_dup.py"
    assert all(item.model_kind == ModelKind.FAKE for item in report.rejected)


def test_generate_tests_refuses_without_a_ready_sandbox(tmp_path):
    root = _project(tmp_path, ["read_item"])
    profile = make_root_profile(root)
    selection = select_targets(profile)
    fake = DeterministicFakeLLM()

    with pytest.raises(SandboxUnavailableError) as excinfo:
        generate_tests(
            profile,
            selection,
            fake,
            sandbox_health=sandbox_health(ready=False),
        )

    assert SandboxHealthFailure.STRONG_TEST_FAILED in excinfo.value.health.failures
    assert fake.calls == []


def test_generate_tests_does_not_write_to_the_project(tmp_path):
    root = _project(tmp_path, ["read_item", "status"])
    profile = make_root_profile(root)
    selection = select_targets(profile)
    before = _snapshot(root)

    generate_tests(
        profile,
        selection,
        DeterministicFakeLLM(),
        sandbox_health=sandbox_health(),
    )

    assert _snapshot(root) == before
    assert not (root / "generated").exists()


def test_generate_tests_warns_when_no_targets_are_selected(tmp_path):
    root = _project(tmp_path, [])
    profile = make_root_profile(root)

    report = generate_tests(
        profile,
        select_targets(profile),
        DeterministicFakeLLM(),
        sandbox_health=sandbox_health(),
    )

    assert report.candidates == []
    assert report.rejected == []
    assert report.trace.targets_considered == 0
    assert report.trace.warnings == ["no targets selected"]
