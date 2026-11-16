"""Sandbox execution for candidate pytest tests.

The public return structure is intentionally frozen: later increments may add
optional fields to these models, but must not rename or remove existing ones.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .container import (
    ContainerConfig,
    SandboxBackend,
    build_run_command,
    check_available,
    container_env,
    container_path,
    inspect_state,
    kill_container,
    new_container_name,
    remove_container,
)

try:  # Optional dependency: psutil gives Windows best-effort limits.
    import psutil
except ImportError:  # pragma: no cover - exercised by environments without psutil
    psutil = None

try:  # POSIX-only hard limits.
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None


RUNNER_VERSION = "0.2.0"


class EnforcementMode(str, Enum):
    ENFORCED = "enforced"
    BEST_EFFORT = "best-effort"


class CoverageSource(str, Enum):
    COVERAGE_PY = "coverage.py"
    SYS_SETTRACE = "sys.settrace"


class TestStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class FailureKind(str, Enum):
    NONE = "none"
    ASSERTION = "assertion"
    EXCEPTION = "exception"
    IMPORT_ERROR = "import_error"
    COLLECTION_ERROR = "collection_error"
    TIMEOUT = "timeout"
    RUNNER_ERROR = "runner_error"


class FinalStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    FLAKY = "flaky"


class TimeoutPhase(str, Enum):
    COLLECTION = "collection"
    EXECUTION = "execution"
    TEARDOWN = "teardown"


class RunnerStage(str, Enum):
    COLLECTION = "collection"
    EXECUTION = "execution"
    TEARDOWN = "teardown"


class TargetSymbol(BaseModel):
    symbol_id: str
    name: str
    file: Path
    line_start: int
    line_end: int


class ProjectProfile(BaseModel):
    profile_id: str
    root: Path
    test_paths: list[Path]
    target_symbols: list[TargetSymbol] = Field(default_factory=list)
    python_executable: str = Field(default_factory=lambda: sys.executable)
    env: dict[str, str] = Field(default_factory=dict)


class SandboxConfig(BaseModel):
    timeout_seconds: float = 30.0
    memory_limit_mb: int | None = 512
    cpu_limit_seconds: int | None = 30
    double_run: bool = True
    enforcement_mode: EnforcementMode = EnforcementMode.BEST_EFFORT
    platform: str = Field(default_factory=lambda: platform_module.system())
    coverage_source: CoverageSource = CoverageSource.SYS_SETTRACE
    backend: SandboxBackend = SandboxBackend.SUBPROCESS
    container: ContainerConfig | None = None


class ResourceUsage(BaseModel):
    wall_time_ms: int = 0
    cpu_time_seconds: float | None = None
    peak_rss_mb: float | None = None
    limits_hit: list[str] = Field(default_factory=list)
    enforcement_mode: EnforcementMode = EnforcementMode.BEST_EFFORT


class ResourceSummary(BaseModel):
    peak_rss_mb: float | None = None
    cpu_time_seconds: float | None = None
    wall_time_ms: int = 0
    limits_hit: list[str] = Field(default_factory=list)
    enforcement_mode: EnforcementMode = EnforcementMode.BEST_EFFORT
    platform: str = ""


class TimeoutInfo(BaseModel):
    timed_out: bool = False
    timeout_seconds: float = 0.0
    phase: TimeoutPhase = TimeoutPhase.EXECUTION
    process_tree_killed: bool = False


class TimeoutSummary(BaseModel):
    any_timeout: bool = False
    timed_out_test_ids: list[str] = Field(default_factory=list)
    timeout_seconds: float = 0.0


class SymbolCoverage(BaseModel):
    symbol_id: str
    file: str
    line_start: int
    line_end: int
    entry_hit: bool
    executed_lines: list[int]
    coverage_source: CoverageSource


class TestOutcome(BaseModel):
    test_id: str
    test_file: str
    test_name: str
    status: TestStatus
    failure_kind: FailureKind = FailureKind.NONE
    exception_type: str | None = None
    message_digest: str | None = None
    duration_ms: int = 0
    covered_target_symbols: list[SymbolCoverage] = Field(default_factory=list)
    timeout: TimeoutInfo = Field(default_factory=TimeoutInfo)
    resource_usage: ResourceUsage = Field(default_factory=ResourceUsage)
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None


class TestResult(BaseModel):
    test_id: str
    test_file: str
    test_name: str
    target_symbol: str | None = None
    final_status: FinalStatus
    runs: list[TestOutcome] = Field(default_factory=list)
    flaky: bool = False
    failure_kinds: list[FailureKind] = Field(default_factory=list)
    covered_target_symbols_union: list[str] = Field(default_factory=list)
    timeout: TimeoutInfo = Field(default_factory=TimeoutInfo)
    resource_usage: ResourceUsage = Field(default_factory=ResourceUsage)


class FailureDetail(BaseModel):
    run_index: int
    failure_kind: FailureKind
    exception_type: str | None = None
    message_digest: str | None = None


class FlakyRecord(BaseModel):
    test_id: str
    test_file: str
    test_name: str
    run_statuses: list[TestStatus]
    consistent: bool = False
    failure_details_by_run: list[FailureDetail] = Field(default_factory=list)


class ExecutionTrace(BaseModel):
    double_run: bool
    runner_version: str = RUNNER_VERSION
    python_version: str = Field(default_factory=lambda: sys.version.split()[0])
    cwd: str = ""
    env_digest: str = ""
    backend: SandboxBackend = SandboxBackend.SUBPROCESS
    warnings: list[str] = Field(default_factory=list)


class RunnerError(BaseModel):
    stage: RunnerStage
    message: str
    fatal: bool = False


class TestRunResult(BaseModel):
    run_index: int
    started_at: str
    elapsed_ms: int
    exit_code: int
    timed_out: bool
    tests: list[TestOutcome] = Field(default_factory=list)
    resource_usage: ResourceUsage = Field(default_factory=ResourceUsage)
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None


class ExecutionReport(BaseModel):
    run_id: str
    started_at: str
    elapsed_ms: int
    project_profile_id: str
    sandbox_config: SandboxConfig
    runs: list[TestRunResult]
    tests: list[TestResult]
    flaky: list[FlakyRecord]
    resource_summary: ResourceSummary
    timeout_summary: TimeoutSummary
    trace: ExecutionTrace
    runner_errors: list[RunnerError]


class RunAttempt(BaseModel):
    result: TestRunResult
    errors: list[RunnerError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def resolve_profile(profile: ProjectProfile) -> ProjectProfile:
    root = profile.root.expanduser().resolve()
    test_paths = [
        (path if path.is_absolute() else root / path).resolve()
        for path in profile.test_paths
    ]
    target_symbols = [
        target.model_copy(
            update={
                "file": (target.file if target.file.is_absolute() else root / target.file).resolve()
            }
        )
        for target in profile.target_symbols
    ]
    return profile.model_copy(
        update={"root": root, "test_paths": test_paths, "target_symbols": target_symbols}
    )


def resolve_config(config: SandboxConfig) -> SandboxConfig:
    platform_name = platform_module.system()
    if config.backend == SandboxBackend.CONTAINER:
        mode = (
            EnforcementMode.ENFORCED
            if platform_name == "Linux"
            else EnforcementMode.BEST_EFFORT
        )
    elif platform_name == "Windows":
        mode = EnforcementMode.BEST_EFFORT
    elif resource is not None and (config.memory_limit_mb or config.cpu_limit_seconds):
        mode = EnforcementMode.ENFORCED
    else:
        mode = EnforcementMode.BEST_EFFORT
    return config.model_copy(update={"platform": platform_name, "enforcement_mode": mode})


def env_digest(profile: ProjectProfile) -> str:
    payload = json.dumps(
        {"env": dict(sorted(profile.env.items())), "python": profile.python_executable},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _preexec_fn(config: SandboxConfig):
    if resource is None:
        return None

    def _set_limits() -> None:
        # The child is already placed in a new session/process group by
        # start_new_session=True; calling setsid() here again can fail.
        if config.memory_limit_mb:
            limit = config.memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        if config.cpu_limit_seconds:
            resource.setrlimit(
                resource.RLIMIT_CPU,
                (config.cpu_limit_seconds, config.cpu_limit_seconds),
            )

    return _set_limits


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if platform_module.system() == "Windows":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


class _ResourceMonitor:
    """Best-effort resource sampling; optional psutil dependency."""

    def __init__(self, pid: int, config: SandboxConfig) -> None:
        self.pid = pid
        self.config = config
        self.peak_rss_mb: float | None = None
        self.cpu_time_seconds: float | None = None
        self.limits_hit: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: Any = None

    def start(self) -> None:
        if psutil is None:
            return
        try:
            self._process = psutil.Process(self.pid)
        except psutil.Error:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            try:
                processes = [self._process]
                processes.extend(self._process.children(recursive=True))
                rss_bytes = 0
                cpu_seconds = 0.0
                for process in processes:
                    try:
                        rss_bytes += process.memory_info().rss
                        times = process.cpu_times()
                        cpu_seconds += times.user + times.system
                    except psutil.Error:
                        continue
                peak_rss_mb = rss_bytes / (1024 * 1024)
                if self.peak_rss_mb is None or peak_rss_mb > self.peak_rss_mb:
                    self.peak_rss_mb = peak_rss_mb
                if self.cpu_time_seconds is None or cpu_seconds > self.cpu_time_seconds:
                    self.cpu_time_seconds = cpu_seconds
                if self.config.memory_limit_mb and peak_rss_mb > self.config.memory_limit_mb:
                    self.limits_hit.add("memory")
                    self._kill()
                    return
                if self.config.cpu_limit_seconds and cpu_seconds > self.config.cpu_limit_seconds:
                    self.limits_hit.add("cpu")
                    self._kill()
                    return
            except psutil.Error:
                return

    def _kill(self) -> None:
        try:
            for child in self._process.children(recursive=True):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            self._process.kill()
        except psutil.Error:
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


def _split_nodeid(nodeid: str) -> tuple[str, str]:
    if "::" in nodeid:
        file_part, name = nodeid.split("::", 1)
        return file_part, name
    return nodeid, nodeid


def _build_outcomes(
    plugin_data: dict[str, Any],
    config: SandboxConfig,
    timed_out: bool,
    monitor: _ResourceMonitor,
) -> list[TestOutcome]:
    outcomes: list[TestOutcome] = []
    tests_data = plugin_data.get("tests", {})
    for test_id in sorted(tests_data):
        item = tests_data[test_id]
        raw_status = item.get("status", "error")
        if raw_status == "pending":
            if timed_out:
                status = TestStatus.TIMEOUT
                failure_kind = FailureKind.TIMEOUT
            else:
                status = TestStatus.ERROR
                failure_kind = FailureKind.RUNNER_ERROR
        else:
            try:
                status = TestStatus(raw_status)
            except ValueError:
                status = TestStatus.ERROR
            try:
                failure_kind = FailureKind(item.get("failure_kind", "none"))
            except ValueError:
                failure_kind = FailureKind.RUNNER_ERROR

        covered = [
            SymbolCoverage(
                symbol_id=symbol["symbol_id"],
                file=symbol["file"],
                line_start=int(symbol["line_start"]),
                line_end=int(symbol["line_end"]),
                entry_hit=bool(symbol.get("entry_hit", False)),
                executed_lines=[int(line) for line in symbol.get("executed_lines", [])],
                coverage_source=CoverageSource(
                    symbol.get("coverage_source", config.coverage_source.value)
                ),
            )
            for symbol in item.get("covered_symbols", [])
        ]

        is_timeout = status == TestStatus.TIMEOUT and timed_out
        outcomes.append(
            TestOutcome(
                test_id=test_id,
                test_file=item.get("test_file", ""),
                test_name=item.get("test_name", test_id),
                status=status,
                failure_kind=failure_kind,
                exception_type=item.get("exception_type"),
                message_digest=item.get("message_digest"),
                duration_ms=int(item.get("duration_ms", 0)),
                covered_target_symbols=covered,
                timeout=TimeoutInfo(
                    timed_out=is_timeout,
                    timeout_seconds=config.timeout_seconds,
                    phase=TimeoutPhase(item.get("timeout_phase", "execution")),
                    process_tree_killed=is_timeout
                    or (raw_status == "pending" and bool(monitor.limits_hit)),
                ),
                resource_usage=ResourceUsage(
                    wall_time_ms=int(item.get("duration_ms", 0)),
                    enforcement_mode=config.enforcement_mode,
                ),
            )
        )

    current_test = plugin_data.get("current_test")
    if current_test and current_test not in tests_data:
        test_file, test_name = _split_nodeid(current_test)
        if timed_out:
            outcomes.append(
                TestOutcome(
                    test_id=current_test,
                    test_file=test_file,
                    test_name=test_name,
                    status=TestStatus.TIMEOUT,
                    failure_kind=FailureKind.TIMEOUT,
                    timeout=TimeoutInfo(
                        timed_out=True,
                        timeout_seconds=config.timeout_seconds,
                        phase=TimeoutPhase.EXECUTION,
                        process_tree_killed=True,
                    ),
                    resource_usage=ResourceUsage(enforcement_mode=config.enforcement_mode),
                )
            )
        elif monitor.limits_hit:
            outcomes.append(
                TestOutcome(
                    test_id=current_test,
                    test_file=test_file,
                    test_name=test_name,
                    status=TestStatus.ERROR,
                    failure_kind=FailureKind.RUNNER_ERROR,
                    timeout=TimeoutInfo(
                        timed_out=False,
                        timeout_seconds=config.timeout_seconds,
                        phase=TimeoutPhase.EXECUTION,
                        process_tree_killed=True,
                    ),
                    resource_usage=ResourceUsage(enforcement_mode=config.enforcement_mode),
                )
            )
    return outcomes


def _run_once_subprocess(
    profile: ProjectProfile,
    config: SandboxConfig,
    *,
    run_index: int = 0,
    artifact_dir: Path | None = None,
) -> RunAttempt:
    profile = resolve_profile(profile)
    config = resolve_config(config)
    artifact_dir = (
        Path(artifact_dir) if artifact_dir is not None else Path(tempfile.mkdtemp(prefix="testlens_run_"))
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    errors: list[RunnerError] = []
    if psutil is None:
        warnings.append("psutil is not installed; memory/CPU limits are best-effort only")
    if platform_module.system() == "Windows":
        warnings.append("Windows: timeout is enforced; memory/CPU limits are best-effort")

    plugin_config_path = artifact_dir / "plugin_config.json"
    plugin_output_path = artifact_dir / "plugin_output.json"
    pytest_config_path = artifact_dir / "pytest.ini"
    pytest_config_path.write_text("[pytest]\naddopts =\n", encoding="utf-8")
    plugin_path = Path(__file__).with_name("_pytest_plugin.py")

    plugin_config = {
        "project_root": str(profile.root),
        "test_paths": [str(path) for path in profile.test_paths],
        "target_symbols": [
            {
                "symbol_id": target.symbol_id,
                "name": target.name,
                "file": str(target.file),
                "line_start": target.line_start,
                "line_end": target.line_end,
            }
            for target in profile.target_symbols
        ],
        "output_path": str(plugin_output_path),
        "pytest_config": str(pytest_config_path),
        "timeout_seconds": config.timeout_seconds,
    }
    plugin_config_path.write_text(json.dumps(plugin_config, indent=2), encoding="utf-8")

    env = os.environ.copy()
    env.update(profile.env)
    python_path_parts = [str(profile.root)]
    if env.get("PYTHONPATH"):
        python_path_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path_parts)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    command = [profile.python_executable, str(plugin_path), str(plugin_config_path)]

    popen_kwargs: dict[str, Any] = {
        "cwd": str(profile.root),
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if platform_module.system() == "Windows":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
        preexec = _preexec_fn(config)
        if preexec is not None:
            popen_kwargs["preexec_fn"] = preexec

    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(
            RunnerError(
                stage=RunnerStage.EXECUTION,
                message=f"could not start sandbox process: {exc}",
                fatal=True,
            )
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return RunAttempt(
            result=TestRunResult(
                run_index=run_index,
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                exit_code=-1,
                timed_out=False,
                tests=[],
                resource_usage=ResourceUsage(
                    wall_time_ms=elapsed_ms,
                    enforcement_mode=config.enforcement_mode,
                ),
            ),
            errors=errors,
            warnings=warnings,
        )

    monitor = _ResourceMonitor(process.pid, config)
    monitor.start()

    timed_out = False
    stdout = ""
    stderr = ""
    try:
        stdout, stderr = process.communicate(timeout=config.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        monitor.limits_hit.add("timeout")
        _kill_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    finally:
        monitor.stop()

    if process.returncode is not None and process.returncode < 0:
        warnings.append(f"pytest process terminated by signal {-process.returncode}")

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    stdout_artifact = artifact_dir / "run_stdout.txt"
    stderr_artifact = artifact_dir / "run_stderr.txt"
    stdout_artifact.write_text(stdout or "", encoding="utf-8")
    stderr_artifact.write_text(stderr or "", encoding="utf-8")

    plugin_data: dict[str, Any] = {}
    if plugin_output_path.exists():
        try:
            plugin_data = json.loads(plugin_output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(
                RunnerError(
                    stage=RunnerStage.EXECUTION,
                    message=f"invalid plugin output: {exc}",
                    fatal=True,
                )
            )
    else:
        errors.append(
            RunnerError(
                stage=RunnerStage.EXECUTION,
                message="plugin produced no output; see stderr artifact",
                fatal=True,
            )
        )

    for item in plugin_data.get("collection_errors", []):
        errors.append(
            RunnerError(
                stage=RunnerStage.COLLECTION,
                message=f"{item.get('nodeid', '?')}: {item.get('message', 'collection error')}",
                fatal=False,
            )
        )

    outcomes = _build_outcomes(plugin_data, config, timed_out, monitor)
    if timed_out and not outcomes:
        errors.append(
            RunnerError(
                stage=RunnerStage.COLLECTION,
                message="pytest timed out before reporting a test",
                fatal=False,
            )
        )

    resource_usage = ResourceUsage(
        wall_time_ms=elapsed_ms,
        cpu_time_seconds=monitor.cpu_time_seconds,
        peak_rss_mb=monitor.peak_rss_mb,
        limits_hit=sorted(monitor.limits_hit),
        enforcement_mode=config.enforcement_mode,
    )
    result = TestRunResult(
        run_index=run_index,
        started_at=started_at,
        elapsed_ms=elapsed_ms,
        exit_code=process.returncode if process.returncode is not None else -1,
        timed_out=timed_out,
        tests=outcomes,
        resource_usage=resource_usage,
        stdout_artifact=str(stdout_artifact),
        stderr_artifact=str(stderr_artifact),
    )
    return RunAttempt(result=result, errors=errors, warnings=warnings)


@dataclass
class _LimitView:
    limits_hit: set[str] = field(default_factory=set)


def _empty_attempt(
    *,
    run_index: int,
    started_at: str,
    elapsed_ms: int,
    config: SandboxConfig,
    errors: list[RunnerError],
    warnings: list[str] | None = None,
) -> RunAttempt:
    return RunAttempt(
        result=TestRunResult(
            run_index=run_index,
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            exit_code=-1,
            timed_out=False,
            tests=[],
            resource_usage=ResourceUsage(
                wall_time_ms=elapsed_ms,
                enforcement_mode=config.enforcement_mode,
            ),
        ),
        errors=errors,
        warnings=list(warnings or []),
    )


def _prepare_container_plugin_files(
    profile: ProjectProfile,
    config: SandboxConfig,
    artifact_dir: Path,
) -> Path:
    container_config = config.container
    if container_config is None:
        raise ValueError("container backend requires container configuration")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    project_root = profile.root.expanduser().resolve()
    plugin_path = artifact_dir / "_pytest_plugin.py"
    shutil.copy2(Path(__file__).with_name("_pytest_plugin.py"), plugin_path)
    pytest_config_path = artifact_dir / "pytest.ini"
    pytest_config_path.write_text("[pytest]\naddopts =\n", encoding="utf-8")
    plugin_config_path = artifact_dir / "plugin_config.json"
    container_test_paths: list[str] = []
    external_dir = artifact_dir / "_external_tests"
    for index, path in enumerate(profile.test_paths):
        try:
            container_test_paths.append(container_path(path, project_root))
        except ValueError:
            # Repro scripts live outside the project by design. Copy them into
            # the writable artifacts mount instead of mounting their host
            # directory.
            external_dir.mkdir(parents=True, exist_ok=True)
            destination = external_dir / f"{index}_{path.name}"
            shutil.copy2(path, destination)
            container_test_paths.append(
                f"/artifacts/_external_tests/{destination.name}"
            )
    plugin_config = {
        "project_root": "/project",
        "test_paths": container_test_paths,
        "target_symbols": [
            {
                "symbol_id": target.symbol_id,
                "name": target.name,
                "file": container_path(target.file, project_root),
                "line_start": target.line_start,
                "line_end": target.line_end,
            }
            for target in profile.target_symbols
        ],
        "output_path": "/artifacts/plugin_output.json",
        "pytest_config": "/artifacts/pytest.ini",
        "timeout_seconds": config.timeout_seconds,
    }
    plugin_config_path.write_text(
        json.dumps(plugin_config, indent=2), encoding="utf-8"
    )
    return plugin_config_path


def _run_once_container(
    profile: ProjectProfile,
    config: SandboxConfig,
    *,
    run_index: int,
    artifact_dir: Path,
) -> RunAttempt:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    errors: list[RunnerError] = []
    warnings: list[str] = []
    container_config = config.container
    if container_config is None:
        errors.append(
            RunnerError(
                stage=RunnerStage.EXECUTION,
                message="container backend requires container configuration",
                fatal=True,
            )
        )
        return _empty_attempt(
            run_index=run_index,
            started_at=started_at,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            config=config,
            errors=errors,
        )

    availability = check_available(container_config)
    if not availability.available:
        errors.append(
            RunnerError(
                stage=RunnerStage.EXECUTION,
                message=(
                    f"container backend unavailable "
                    f"({availability.failure.value if availability.failure else 'unknown'}): "
                    f"{availability.detail}"
                ),
                fatal=True,
            )
        )
        return _empty_attempt(
            run_index=run_index,
            started_at=started_at,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            config=config,
            errors=errors,
        )

    try:
        plugin_config_path = _prepare_container_plugin_files(
            profile, config, artifact_dir
        )
    except (OSError, ValueError) as exc:
        errors.append(
            RunnerError(
                stage=RunnerStage.EXECUTION,
                message=f"could not prepare container sandbox: {exc}",
                fatal=True,
            )
        )
        return _empty_attempt(
            run_index=run_index,
            started_at=started_at,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            config=config,
            errors=errors,
        )

    name = new_container_name()
    command = build_run_command(
        container_config,
        name=name,
        project_root=profile.root.expanduser().resolve(),
        artifacts_root=artifact_dir,
        memory_limit_mb=config.memory_limit_mb,
        cpu_limit_seconds=config.cpu_limit_seconds,
        env=container_env(profile.env),
    )
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "start_new_session": True,
    }
    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(
            RunnerError(
                stage=RunnerStage.EXECUTION,
                message=f"could not start container runtime: {exc}",
                fatal=True,
            )
        )
        return _empty_attempt(
            run_index=run_index,
            started_at=started_at,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            config=config,
            errors=errors,
        )

    timed_out = False
    limits_hit: set[str] = set()
    stdout = ""
    stderr = ""
    try:
        try:
            stdout, stderr = process.communicate(timeout=config.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            limits_hit.add("timeout")
            kill_container(container_config, name)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    stdout, stderr = "", ""
        state = inspect_state(container_config, name)
        if state.get("OOMKilled"):
            limits_hit.add("memory")
    finally:
        remove_container(container_config, name)

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    exit_code = process.returncode if process.returncode is not None else -1
    if exit_code in (125, 126, 127):
        detail = (stderr or stdout or "").strip()
        errors.append(
            RunnerError(
                stage=RunnerStage.EXECUTION,
                message=(
                    f"container runtime failed with exit code {exit_code}: "
                    f"{detail[-1000:] or 'no runtime output'}"
                ),
                fatal=True,
            )
        )

    stdout_artifact = artifact_dir / "run_stdout.txt"
    stderr_artifact = artifact_dir / "run_stderr.txt"
    stdout_artifact.write_text(stdout or "", encoding="utf-8")
    stderr_artifact.write_text(stderr or "", encoding="utf-8")

    plugin_output_path = artifact_dir / "plugin_output.json"
    plugin_data: dict[str, Any] = {}
    if plugin_output_path.exists():
        try:
            plugin_data = json.loads(
                plugin_output_path.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError) as exc:
            errors.append(
                RunnerError(
                    stage=RunnerStage.EXECUTION,
                    message=f"invalid plugin output: {exc}",
                    fatal=True,
                )
            )
    else:
        errors.append(
            RunnerError(
                stage=RunnerStage.EXECUTION,
                message="plugin produced no output; see stderr artifact",
                fatal=True,
            )
        )

    for item in plugin_data.get("collection_errors", []):
        errors.append(
            RunnerError(
                stage=RunnerStage.COLLECTION,
                message=(
                    f"{item.get('nodeid', '?')}: "
                    f"{item.get('message', 'collection error')}"
                ),
                fatal=False,
            )
        )

    limit_view = _LimitView(limits_hit=limits_hit)
    outcomes = _build_outcomes(plugin_data, config, timed_out, limit_view)
    if timed_out and not outcomes:
        errors.append(
            RunnerError(
                stage=RunnerStage.COLLECTION,
                message="pytest timed out before reporting a test",
                fatal=False,
            )
        )

    resource_usage = ResourceUsage(
        wall_time_ms=elapsed_ms,
        cpu_time_seconds=None,
        peak_rss_mb=None,
        limits_hit=sorted(limits_hit),
        enforcement_mode=config.enforcement_mode,
    )
    result = TestRunResult(
        run_index=run_index,
        started_at=started_at,
        elapsed_ms=elapsed_ms,
        exit_code=exit_code,
        timed_out=timed_out,
        tests=outcomes,
        resource_usage=resource_usage,
        stdout_artifact=str(stdout_artifact),
        stderr_artifact=str(stderr_artifact),
    )
    return RunAttempt(result=result, errors=errors, warnings=warnings)


def run_once(
    profile: ProjectProfile,
    config: SandboxConfig,
    *,
    run_index: int = 0,
    artifact_dir: Path | None = None,
) -> RunAttempt:
    config = resolve_config(config)
    if config.backend == SandboxBackend.CONTAINER:
        resolved = resolve_profile(profile)
        target_dir = (
            Path(artifact_dir)
            if artifact_dir is not None
            else Path(tempfile.mkdtemp(prefix="testlens_run_"))
        )
        return _run_once_container(
            resolved,
            config,
            run_index=run_index,
            artifact_dir=target_dir,
        )
    return _run_once_subprocess(
        profile,
        config,
        run_index=run_index,
        artifact_dir=artifact_dir,
    )
