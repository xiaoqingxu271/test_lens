# TestLens v2 specification — 2A container sandbox backend

**Status: ACCEPTED 2026-11-16. `CLAUDE.md` carries the normative summary; this
document holds the full design.**

This document specifies only increment 2A: an opt-in container execution
backend. It does not specify the real-model client, `LLM_INDEPENDENT`
findings, or new rules/operators. Those remain separate increments.

## 1. Goal

Run candidate pytest tests in a container with enforced network, filesystem,
process, memory, and CPU boundaries, while preserving every existing v1
contract and default behavior.

Success means:

- the default `subprocess` backend is unchanged;
- the same `ExecutionReport` contract is returned by both backends, with new
  optional fields only;
- container unavailability fails loudly and is never silently replaced by
  `subprocess`;
- the deterministic evaluation baseline can run with either backend and
  produce the same fixture metrics;
- Linux CI proves `enforcement_mode=enforced` for the container backend.

## 2. Non-goals for 2A

- Windows containers, Kubernetes, or remote execution.
- Per-run `pip install` or automatic dependency resolution.
- A network allowlist. Container runs have no network at all.
- Custom seccomp/AppArmor profiles beyond the runtime defaults.
- GPU, privileged mode, or host device access.
- Replacing `subprocess` as the default backend.
- Storing backend-specific SQLite tables. The full report payload remains the
  source of truth; normalized tables do not change.

## 3. Frozen-contract rules

`CLAUDE.md` decision 5 allows optional fields to be added but forbids renaming
or removing existing fields. 2A therefore adds only:

```python
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
    user: str = "auto"  # resolved to the host uid:gid on Linux


class SandboxConfig(BaseModel):
    # existing fields unchanged
    backend: SandboxBackend = SandboxBackend.SUBPROCESS
    container: ContainerConfig | None = None
```

Additive fields elsewhere:

- `ExecutionTrace.backend: SandboxBackend = SUBPROCESS`
- `SandboxHealth.backend: SandboxBackend = SUBPROCESS`
- `check_sandbox_ready(..., config: SandboxConfig | None = None)` — the probe
  uses the configured backend; defaults unchanged.
- new `SandboxHealthFailure` members:
  `CONTAINER_RUNTIME_UNAVAILABLE`, `CONTAINER_IMAGE_UNAVAILABLE`,
  `CONTAINER_PROBE_FAILED`, `CONTAINER_UNSUPPORTED_PLATFORM`.

Existing report fields, enum values, and runner semantics stay unchanged.
`RUNNER_VERSION` becomes `0.2.0` when the backend lands.

## 4. Backend architecture

Keep `sandbox/runner.py` as the public entry point. `run_once` selects a
transport without changing its signature:

```
double_run.execute(profile, config)
  -> runner.run_once(profile, config, run_index, artifact_dir)
       -> resolve_config(config)
       -> subprocess backend: existing host process path
       -> container backend: prepare container plugin files, docker run
       -> _build_outcomes(...)             # unchanged
```

Modules:

- `sandbox/container.py` — backend/runtime enums, `ContainerConfig`,
  availability probe, command construction, inspect/kill/remove helpers.
- `sandbox/runner.py` — the existing subprocess implementation is retained as
  `_run_once_subprocess`; `_run_once_container` uses the Docker CLI; `run_once`
  dispatches explicitly.
- `sandbox/Dockerfile` — default Python 3.12 + pytest image.

No Docker SDK dependency. The backend shells out to the pinned runtime CLI so
v1's dependency footprint stays unchanged. All commands use argument lists,
never `shell=True`.

The backend selection flows through every existing caller without new
pipeline stages:

- `double_run.execute` runs each attempt in a separate container.
- `mutation.score.score_test` runs each mutant with `double_run=false` through
  the same backend; one mutant gets one container.
- `audit.repro.run_repro` uses the same double-run path and therefore the same
  backend.
- `pipeline.run_test_pipeline` passes its `SandboxConfig` through unchanged.

Container startup is paid per run and per mutant. 2A accepts the cost and
records elapsed time in the existing trace; it does not pool or reuse
containers.

### Image contract

`sandbox/Dockerfile` builds the default image:

- Linux, Python 3.12, non-root-capable;
- pytest pinned to the evaluation version;
- no framework, no project dependencies, no network tools;
- CI pins the base image by digest.

The default image is sufficient for the vendored fixtures and the sandbox
health probe. A target project with third-party dependencies must supply its
own image through `ContainerConfig.image`; TestLens never installs packages at
run time.

## 5. Sandbox bundle and mounts

The runner writes its per-run plugin files into the existing artifact
directory and mounts that directory plus the project:

```
artifact_dir/
  _pytest_plugin.py
  plugin_config.json
  pytest.ini
  _external_tests/       # repro scripts copied in when outside the project
```

Container layout:

| Host path | Container path | Mode |
|---|---|---|
| `profile.root` (isolated copy) | `/project` | read-only |
| `artifact_dir` | `/artifacts` | read-write |
| — | `/tmp` | `tmpfs`, `tmpfs_mb` |

- The target project is never mounted. The pipeline already materialized an
  isolated copy; direct callers mount their own project root read-only.
- The project copy is read-only; pytest cache is disabled
  (`-p no:cacheprovider`) and bytecode writes are redirected to `/tmp`.
- The plugin config uses container paths (`/project`, `/artifacts`); the host
  runner reads results back from `artifact_dir`.
- Reproduction scripts live outside the project by design. They are copied
  into `artifact_dir/_external_tests/` and run from `/artifacts/...` rather
  than mounting a host directory.
- Each run gets a fresh artifact directory. It is deleted with the existing
  temporary-directory cleanup.

## 6. Isolation policy

Every container run uses:

```
--name testlens-<run_id>
--network none
--read-only
--tmpfs /tmp:rw,size=<tmpfs_mb>m,mode=1777
--cap-drop ALL
--security-opt no-new-privileges
--pids-limit <pids_limit>
--memory <memory_limit_mb>m
--memory-swap <memory_limit_mb>m
--ulimit cpu=<cpu_limit_seconds>:<cpu_limit_seconds>
--user <resolved uid:gid>
-v <project>:/project:ro
-v <artifacts>:/artifacts:rw
-w /project
```

The CPU limit maps to the POSIX CPU-time ulimit, matching v1's
`RLIMIT_CPU` semantics. `--cpus` is not used: it expresses a share, not a
total CPU-time budget. `--memory-swap` equals `--memory` so the container
cannot swap around the memory cap.

The container is named but not run with `--rm`, because `State.OOMKilled`
must be inspected after exit. The runner always removes it with `docker rm -f`
in a `finally` block.

Container backend support in 2A is limited to Linux hosts. On Windows/macOS
hosts it reports `CONTAINER_UNSUPPORTED_PLATFORM`; it never pretends that
Docker Desktop path translation provides the same guarantees.

Environment:

- The default container environment is an allowlist: `PATH`,
  `PYTHONPATH=/project`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONHASHSEED=0`,
  `PYTHONPYCACHEPREFIX=/tmp/pycache`.
- `profile.env` entries are passed explicitly and are opt-in per profile.
- The host environment is never inherited wholesale. This closes the v1
  limitation where sandbox subprocesses inherit parent variables.
- No secrets from the host are mounted or passed.

Timeout handling:

- The host enforces `timeout_seconds` with `docker kill` on expiry, then
  inspects and removes the container.
- `TimeoutInfo.timed_out=true`, `process_tree_killed=true`,
  `limits_hit` contains `timeout`.
- Containers are removed even on runner exceptions.

## 7. Resource accounting

The container runtime enforces the limits, so `enforcement_mode=ENFORCED` is
reported truthfully when the runtime accepted memory/CPU/PID limits.

- `limits_hit` is derived from the runtime state and exit codes:
  - OOM kill (`OOMKilled=true`, exit 137) → `memory`;
  - timeout kill by the host → `timeout`;
  - CPU quota exhaustion is reported by the runtime as CPU throttling; 2A does
    not treat ordinary throttling as a limit hit.
- `peak_rss_mb` and `cpu_time_seconds` are reported as `None` in 2A unless a
  runtime-supported measurement is available. No sampled or estimated values
  are fabricated.
- `process_tree_killed=true` when the whole container is killed.

This is a deliberate difference from the `subprocess` backend, whose `psutil`
sampling provides approximate peak values. Evaluation reports state the
backend so the two are never silently compared.

## 8. Availability and failure semantics

Before the first run, the backend must pass `available()`:

- runtime CLI exists;
- daemon responds;
- the configured image exists locally.

Unavailability produces an enumerated failure and a fatal `RunnerError`:

- `CONTAINER_RUNTIME_UNAVAILABLE`
- `CONTAINER_IMAGE_UNAVAILABLE`
- `CONTAINER_PROBE_FAILED`

Rules:

- **No silent fallback.** If `backend=container` was requested, the run never
  executes on the host.
- The sandbox health gate uses the configured backend. If the probe fails,
  `generate_tests` raises `SandboxUnavailableError` exactly as in v1.
- Runtime/container errors are recorded as `RunnerError` with stage and
  fatal flag; they are never converted into passing or skipped tests.
- A missing plugin output is a fatal runner error, not an empty passing run.

## 9. CLI and evaluation surface

New options on `testlens tests`, `testlens audit`, and `testlens eval`:

```
--sandbox-backend {subprocess,container}   # default: subprocess
--sandbox-image IMAGE                      # required for container
--sandbox-runtime {docker}                 # default: docker
```

- `--sandbox-image` without `--sandbox-backend container` is a usage error.
- The CLI never auto-detects a backend.
- `python -m evaluation` gains the same options.
- Reports record `sandbox_config.backend`,
  `sandbox_config.container.image`, and enforcement mode.

The evaluation baseline keeps `subprocess` as its default. A second CI run
uses the container backend and must reproduce the fixture metrics.

## 10. Testing plan

Unit and integration tests:

1. **Contract tests** — new optional fields present; all v1 fields unchanged;
   default `backend=subprocess`.
2. **Backend selection** — explicit container selection; no fallback when the
   runtime is unavailable; usage errors for inconsistent options.
3. **Health probe** — strong/weak probe passes through the container backend;
   failure categories are enumerated.
4. **Parity** — the pipeline fixture produces the same test ids, discard
   reasons, and mutation scores under both backends.
5. **Isolation** — network access fails; writes outside `/project`,
   `/artifacts`, and `/tmp` fail; host environment secrets are absent; the
   mounted project copy is unchanged (hash before/after).
6. **Limits** — timeout kills and removes the container; OOM maps to `memory`;
   `pids_limit` prevents process explosion.
7. **Cleanup** — no containers or bundles remain after success, failure, and
   timeout.
8. **Double-run** — two independent containers, no shared writable state.

Container tests are marked `@pytest.mark.container` and skip when Docker is
not available. They are required in CI, optional for local Windows
development.

CI changes:

- Existing `linux` matrix job stays host-only.
- New `container` job on `ubuntu-latest`:
  - builds the default image from `sandbox/Dockerfile`;
  - runs `pytest -m container`;
  - runs `python -m evaluation --sandbox-backend container --sandbox-image ...`;
  - asserts `backend=container`, `enforcement_mode=enforced`,
    `double_run=true`, and fixture parity.

## 11. Versioning and migration

- Package version: v2 development continues from 1.0.0; the backend ships in
  `2.0.0`, not a v1 patch.
- `RUNNER_VERSION` → `0.2.0`.
- SQLite schema stays at version 1. The backend is inside the stored JSON
  payload and does not need a normalized column in 2A.
- v1 reports and databases remain readable.
- `subprocess` remains the default; a v2 report must state the backend
  explicitly.

## 12. Confirmed decisions (2026-11-16)

1. **Runtime scope** — Docker CLI only in 2A; Podman detection deferred.
2. **Image supply** — ship a Dockerfile for the default test image, but allow
   any operator-supplied image; pin the CI image by digest where practical.
3. **Filesystem layout** — project copy read-only, artifacts read-write,
   `/tmp` tmpfs.
4. **Environment** — allowlist only; `profile.env` opt-in; no host environment
   inheritance.
5. **Resource metrics** — enforce limits but report `peak_rss_mb` and
   `cpu_time_seconds` as `None` when the runtime cannot provide them; never
   estimate.
6. **Default backend** — `subprocess` remains the default everywhere;
   container is explicit and required only for the release/CI baseline.

## 13. Acceptance criteria for 2A

- All existing tests pass with no changes and no container dependency.
- New container tests pass in Linux CI.
- The evaluation baseline runs under the container backend with the same
  fixture metrics as the `subprocess` baseline.
- No silent fallback exists on any path, including health checks and repro.
- The project directory and host environment are provably isolated by tests.
- `CLAUDE.md` is updated with the confirmed decisions before implementation
  begins.
