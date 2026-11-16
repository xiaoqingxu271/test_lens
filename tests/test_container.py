from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from sandbox import double_run, runner
from sandbox.container import (
    ContainerConfig,
    ContainerFailure,
    SandboxBackend,
    check_available,
)
from sandbox.health import SandboxHealthFailure, check_sandbox_ready
from sandbox.runner import (
    EnforcementMode,
    FinalStatus,
    ProjectProfile,
    SandboxConfig,
    TargetSymbol,
)

IMAGE = os.environ.get("TESTLENS_CONTAINER_IMAGE", "testlens-sandbox:1")
pytestmark = [pytest.mark.container]


def _available() -> bool:
    if platform.system() != "Linux" or shutil.which("docker") is None:
        return False
    return check_available(ContainerConfig(image=IMAGE)).available


requires_container = pytest.mark.skipif(
    not _available(),
    reason=(
        "requires a Linux host with Docker and the image set by "
        "TESTLENS_CONTAINER_IMAGE"
    ),
)


def _config(
    *,
    timeout_seconds: float = 30.0,
    double_run: bool = True,
) -> SandboxConfig:
    return SandboxConfig(
        timeout_seconds=timeout_seconds,
        memory_limit_mb=256,
        cpu_limit_seconds=30,
        double_run=double_run,
        backend=SandboxBackend.CONTAINER,
        container=ContainerConfig(image=IMAGE),
    )


def _profile(tmp_path: Path, test_source: str) -> ProjectProfile:
    app = tmp_path / "app.py"
    app.write_text(
        "def classify(x):\n"
        "    if x > 0:\n"
        "        return 1\n"
        "    return 0\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_probe.py"
    test_file.write_text(test_source, encoding="utf-8")
    return ProjectProfile(
        profile_id="container-test",
        root=tmp_path,
        test_paths=[test_file],
        target_symbols=[
            TargetSymbol(
                symbol_id="app.py::classify",
                name="classify",
                file=app,
                line_start=1,
                line_end=4,
            )
        ],
    )


@requires_container
def test_container_health_probe_passes():
    health = check_sandbox_ready(
        timeout_seconds=60.0,
        config=_config(double_run=True),
    )

    assert health.ready, health.details
    assert health.backend is SandboxBackend.CONTAINER
    assert health.enforcement_mode is EnforcementMode.ENFORCED
    assert health.double_run is True


@requires_container
def test_container_blocks_network(tmp_path):
    profile = _profile(
        tmp_path,
        "import socket\n"
        "import pytest\n\n\n"
        "def test_network_is_blocked():\n"
        "    with pytest.raises(OSError):\n"
        "        socket.create_connection(('1.1.1.1', 80), timeout=1)\n",
    )

    report = double_run.execute(profile, _config())

    assert report.sandbox_config.backend is SandboxBackend.CONTAINER
    assert report.trace.backend is SandboxBackend.CONTAINER
    assert report.tests[0].final_status is FinalStatus.PASSED
    assert all(run.status.value == "passed" for run in report.tests[0].runs)


@requires_container
def test_container_does_not_inherit_host_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTLENS_HOST_SECRET", "do-not-leak")
    profile = _profile(
        tmp_path,
        "import os\n\n\n"
        "def test_environment_is_allowlisted():\n"
        "    assert 'TESTLENS_HOST_SECRET' not in os.environ\n"
        "    assert os.environ['PYTHONPATH'] == '/project'\n",
    )

    report = double_run.execute(profile, _config())

    assert report.tests[0].final_status is FinalStatus.PASSED


@requires_container
def test_container_timeout_is_killed_and_reported(tmp_path):
    profile = _profile(
        tmp_path,
        "import time\n\n\n"
        "def test_slow():\n"
        "    time.sleep(60)\n",
    )

    report = double_run.execute(
        profile, _config(timeout_seconds=10.0, double_run=False)
    )

    assert report.timeout_summary.any_timeout is True
    assert "timeout" in report.resource_summary.limits_hit
    assert report.tests[0].final_status is FinalStatus.TIMEOUT
    assert report.tests[0].timeout.process_tree_killed is True


@requires_container
def test_container_is_removed_after_execution(tmp_path):
    profile = _profile(
        tmp_path,
        "def test_ok():\n    assert True\n",
    )

    report = double_run.execute(
        profile, _config(timeout_seconds=20.0, double_run=False)
    )
    assert report.tests[0].final_status is FinalStatus.PASSED

    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=testlens-",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == ""


@requires_container
def test_missing_image_is_reported():
    availability = check_available(
        ContainerConfig(image="testlens-does-not-exist:0")
    )

    assert availability.available is False
    assert availability.failure is ContainerFailure.IMAGE_UNAVAILABLE


@pytest.mark.skipif(
    platform.system() == "Linux",
    reason="the unsupported-platform check applies to non-Linux hosts",
)
def test_container_backend_rejects_non_linux_hosts(tmp_path):
    health = check_sandbox_ready(
        config=_config(),
    )

    assert health.ready is False
    assert health.backend is SandboxBackend.CONTAINER
    assert (
        SandboxHealthFailure.CONTAINER_UNSUPPORTED_PLATFORM in health.failures
    )


def test_container_unavailability_never_falls_back(tmp_path):
    profile = _profile(
        tmp_path,
        "def test_ok():\n    assert True\n",
    )

    unavailable = SandboxConfig(
        timeout_seconds=10.0,
        double_run=False,
        backend=SandboxBackend.CONTAINER,
        container=ContainerConfig(image="testlens-does-not-exist:0"),
    )
    attempt = runner.run_once(profile, unavailable)

    assert attempt.result.tests == []
    assert attempt.result.exit_code == -1
    assert any(error.fatal for error in attempt.errors)
    assert not any(
        "subprocess" in error.message for error in attempt.errors
    )
