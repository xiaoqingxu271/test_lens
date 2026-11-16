"""Container sandbox helpers.

The container backend is opt-in. It shells out to the runtime CLI rather than
depending on a Docker SDK, so the v1 dependency footprint is unchanged. This
module deliberately has no dependency on :mod:`sandbox.runner`; the runner
imports these pieces.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

CONTAINER_NAME_PREFIX = "testlens-"


class SandboxBackend(str, Enum):
    SUBPROCESS = "subprocess"
    CONTAINER = "container"


class ContainerRuntime(str, Enum):
    DOCKER = "docker"


class ContainerConfig(BaseModel):
    runtime: ContainerRuntime = ContainerRuntime.DOCKER
    image: str
    pids_limit: int = 128
    tmpfs_mb: int = 64
    user: str = "auto"


class ContainerFailure(str, Enum):
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    IMAGE_UNAVAILABLE = "image_unavailable"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    PROBE_FAILED = "probe_failed"


class ContainerAvailability(BaseModel):
    available: bool
    failure: ContainerFailure | None = None
    detail: str = ""


def resolved_user(config: ContainerConfig) -> str:
    if config.user != "auto":
        return config.user
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        return f"{os.getuid()}:{os.getgid()}"
    return "65534:65534"


def check_available(config: ContainerConfig) -> ContainerAvailability:
    if platform.system() != "Linux":
        return ContainerAvailability(
            available=False,
            failure=ContainerFailure.UNSUPPORTED_PLATFORM,
            detail=(
                "the container backend requires a Linux host; "
                f"current platform is {platform.system()}"
            ),
        )
    executable = shutil.which(config.runtime.value)
    if executable is None:
        return ContainerAvailability(
            available=False,
            failure=ContainerFailure.RUNTIME_UNAVAILABLE,
            detail=f"{config.runtime.value} CLI not found on PATH",
        )
    try:
        result = subprocess.run(
            [executable, "image", "inspect", config.image],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ContainerAvailability(
            available=False,
            failure=ContainerFailure.PROBE_FAILED,
            detail=f"could not query {config.runtime.value}: {exc}",
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return ContainerAvailability(
            available=False,
            failure=ContainerFailure.IMAGE_UNAVAILABLE,
            detail=detail[-1000:] or f"image {config.image!r} is not available",
        )
    return ContainerAvailability(available=True)


def container_path(path: Path, project_root: Path) -> str:
    """Map a host path inside the project root to its container path."""
    relative = path.expanduser().resolve().relative_to(
        project_root.expanduser().resolve()
    )
    return "/project/" + relative.as_posix()


def new_container_name() -> str:
    return CONTAINER_NAME_PREFIX + os.urandom(8).hex()


def build_run_command(
    config: ContainerConfig,
    *,
    name: str,
    project_root: Path,
    artifacts_root: Path,
    memory_limit_mb: int | None,
    cpu_limit_seconds: int | None,
    env: dict[str, str],
) -> list[str]:
    command = [
        config.runtime.value,
        "run",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,size={config.tmpfs_mb}m,mode=1777",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(config.pids_limit),
        "--user",
        resolved_user(config),
        "-v",
        f"{project_root}:/project:ro",
        "-v",
        f"{artifacts_root}:/artifacts:rw",
        "-w",
        "/project",
    ]
    if memory_limit_mb:
        command.extend(
            [
                "--memory",
                f"{memory_limit_mb}m",
                "--memory-swap",
                f"{memory_limit_mb}m",
            ]
        )
    if cpu_limit_seconds:
        command.extend(
            ["--ulimit", f"cpu={cpu_limit_seconds}:{cpu_limit_seconds}"]
        )
    for key in sorted(env):
        command.extend(["-e", f"{key}={env[key]}"])
    command.extend(
        [
            config.image,
            "python",
            "/artifacts/_pytest_plugin.py",
            "/artifacts/plugin_config.json",
        ]
    )
    return command


def container_env(profile_env: dict[str, str]) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH": "/project",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONPYCACHEPREFIX": "/tmp/pycache",
    }
    env.update(profile_env)
    return env


def kill_container(config: ContainerConfig, name: str) -> None:
    _run_quiet([config.runtime.value, "kill", name])


def remove_container(config: ContainerConfig, name: str) -> None:
    _run_quiet([config.runtime.value, "rm", "-f", name])


def inspect_state(config: ContainerConfig, name: str) -> dict:
    try:
        result = subprocess.run(
            [config.runtime.value, "inspect", "--format", "{{json .State}}", name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_quiet(command: list[str]) -> None:
    try:
        subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
