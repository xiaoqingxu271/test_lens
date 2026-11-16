from __future__ import annotations

import pytest

from sandbox import runner
from sandbox.container import (
    ContainerConfig,
    SandboxBackend,
    build_run_command,
    container_env,
    container_path,
)
from sandbox.runner import EnforcementMode, SandboxConfig


def _config(**overrides) -> ContainerConfig:
    values = {"image": "testlens-sandbox:1"}
    values.update(overrides)
    return ContainerConfig(**values)


def test_container_command_enforces_isolation(tmp_path):
    project = tmp_path / "project"
    artifacts = tmp_path / "artifacts"
    project.mkdir()
    artifacts.mkdir()

    command = build_run_command(
        _config(tmpfs_mb=32, pids_limit=64, user="1000:1000"),
        name="testlens-test",
        project_root=project,
        artifacts_root=artifacts,
        memory_limit_mb=128,
        cpu_limit_seconds=5,
        env={"EXTRA": "1"},
    )

    assert command[0] == "docker"
    assert command[1] == "run"
    assert "--rm" not in command
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert command[command.index("--pids-limit") + 1] == "64"
    assert command[command.index("--user") + 1] == "1000:1000"
    assert command[command.index("--memory") + 1] == "128m"
    assert command[command.index("--memory-swap") + 1] == "128m"
    assert command[command.index("--ulimit") + 1] == "cpu=5:5"
    assert f"{project}:/project:ro" in command
    assert f"{artifacts}:/artifacts:rw" in command
    assert "EXTRA=1" in command
    assert command[-4:] == [
        "testlens-sandbox:1",
        "python",
        "/artifacts/_pytest_plugin.py",
        "/artifacts/plugin_config.json",
    ]


def test_container_paths_must_stay_inside_the_project(tmp_path):
    inside = tmp_path / "tests" / "test_app.py"
    inside.parent.mkdir()
    inside.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    assert container_path(inside, tmp_path) == "/project/tests/test_app.py"
    with pytest.raises(ValueError):
        container_path(tmp_path.parent / "outside.py", tmp_path)


def test_container_environment_is_allowlisted():
    env = container_env({"EXTRA": "1"})

    assert env["PYTHONPATH"] == "/project"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["EXTRA"] == "1"
    assert "HOME" not in env
    assert "USER" not in env


def test_container_mode_is_enforced_on_linux(monkeypatch):
    monkeypatch.setattr(runner.platform_module, "system", lambda: "Linux")

    resolved = runner.resolve_config(
        SandboxConfig(
            backend=SandboxBackend.CONTAINER,
            container=_config(),
        )
    )

    assert resolved.platform == "Linux"
    assert resolved.enforcement_mode is EnforcementMode.ENFORCED


def test_external_repro_scripts_are_copied_into_artifacts(tmp_path):
    import json

    from sandbox.runner import ProjectProfile

    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "repro" / "test_repro_finding.py"
    external.parent.mkdir()
    external.write_text(
        "from app import value\n\n\ndef test_repro():\n    assert value()\n",
        encoding="utf-8",
    )
    artifacts = tmp_path / "artifacts"
    profile = ProjectProfile(
        profile_id="repro",
        root=project,
        test_paths=[external],
        target_symbols=[],
    )
    config = SandboxConfig(
        backend=SandboxBackend.CONTAINER,
        container=_config(),
    )

    config_path = runner._prepare_container_plugin_files(
        profile, config, artifacts
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    assert payload["test_paths"] == [
        "/artifacts/_external_tests/0_test_repro_finding.py"
    ]
    assert (artifacts / "_external_tests" / "0_test_repro_finding.py").exists()
