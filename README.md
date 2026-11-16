# TestLens

TestLens is a v1 prototype for small Python web projects (FastAPI/Flask style)
where a model never decides what is true. The model only proposes candidates;
sandboxed execution, mutation scoring, deterministic rules, and an independent
cross-check decide what survives. Every rejection carries an enumerated reason.

Two pipelines share one discipline: **candidate → verification → attribution →
graded output**. A test is kept only when it passes the sandbox and reaches the
mutation-kill threshold. A finding requires rule confirmation, reviewer evidence,
or a sandbox reproduction; a failed cross-check retracts it.

## Install

```bash
python -m pip install -e ".[dev]"
```

Python 3.10+ is required. `psutil` is used for best-effort Windows resource
limits and is included in the `dev` extra.

## Quick start

```bash
# Run the deterministic evaluation baseline on the vendored fixtures.
python -m evaluation

# Audit a project with deterministic rules and the fake triage model.
testlens audit path/to/project --output artifacts/reports --db artifacts/testlens.db

# Generate and validate tests with deterministic fake proposals.
testlens tests path/to/project \
  --proposals proposals.json \
  --output artifacts/reports \
  --db artifacts/testlens.db
```

`proposals.json` maps deterministic request ids to fake responses, for example:

```json
{
  "test_generation::app.py::classify": {
    "tests": [
      {
        "test_file": "generated/test_classify.py",
        "test_name": "test_classify",
        "source": "from app import classify\n\n\ndef test_classify():\n    assert classify(1) == 1\n"
      }
    ]
  }
}
```

Without `--proposals`, the deterministic fake emits a generic smoke proposal.
Those proposals are expected to be filtered by the sandbox, mutation scoring,
and attribution; they are not a quality signal.

`--json` prints the complete report. `--output DIR` writes a timestamped JSON
report under `DIR`. `--db PATH` stores normalized metadata and the full report
payload in SQLite.

## Container sandbox backend (v2)

`subprocess` remains the default. The opt-in container backend runs each test,
double-run attempt, mutant, reproduction, and health probe in a fresh Linux
container with enforced boundaries:

```bash
docker build -t testlens-sandbox:1 sandbox

testlens tests path/to/project \
  --sandbox-backend container \
  --sandbox-image testlens-sandbox:1

python -m evaluation \
  --sandbox-backend container \
  --sandbox-image testlens-sandbox:1
```

The container is run with `--network none`, a read-only root filesystem and
project mount, dropped capabilities, `no-new-privileges`, PID/memory/CPU
limits, and a writable artifacts mount. The environment is an allowlist; host
variables are not inherited. If the runtime or image is unavailable, the run
fails explicitly and never falls back to `subprocess`.

2A supports Docker on Linux hosts only. Windows/macOS hosts report
`CONTAINER_UNSUPPORTED_PLATFORM`. The default image contains Python 3.12 and
pytest; projects with dependencies must supply their own image. Container
memory and CPU values are enforced but reported as `None` when the runtime
cannot measure them; no values are estimated. The design is in
`docs/v2-spec.md`.

Run the container tests with:

```bash
TESTLENS_CONTAINER_IMAGE=testlens-sandbox:1 python -m pytest -m container
```

## Test pipeline

```
select_targets -> generate_tests -> execute_in_sandbox (double-run)
  -> mutate_and_score -> attribute_discard -> TestReport
```

- `select_targets` is AST-only and never imports the project. Coverage can only
  reorder targets; it never removes one or decides acceptance.
- `generate_tests` validates each proposal deterministically: a relative POSIX
  test path, one top-level function matching the declared name, parseable
  source, and a reference to the target. It never executes or writes the
  target project, and refuses to run unless the sandbox acceptance probe is
  ready.
- Candidate tests are materialized in an isolated project copy. Each candidate
  runs in its own subprocess; double-run uses two subprocesses.
- Mutants are generated only inside the target symbol and scored with a fixed
  operator set and seed.
- Every discarded candidate carries one reason: `SYNTAX_ERROR`,
  `ENVIRONMENT_MISSING`, `RUNTIME_ERROR_ON_HAPPY`, `ASSERTION_TOO_WEAK`,
  `TARGET_MISMATCH`, or `FLAKY`. Flaky tests also appear in the report's
  `flaky` list and are excluded from kept tests and pass-rate math.

Failed tests are never retried. A test that fails once and passes once is
flaky, not fixed.

## Audit pipeline

```
rule_scan -> llm_triage -> cross_check -> sandbox_repro -> grade_and_emit
```

- `rule_scan` is deterministic: R001 mutable defaults, R002 local `None`
  dereference, R003 unreachable code, R004 bare `except`, R005 resource opened
  without `with`.
- Triage may only complete a trigger path and restate the claim. It cannot move
  a line anchor or change a rule.
- Cross-check runs the deterministic rule again and requires the same span. An
  independent reviewer handles semantic claims and must return structured
  evidence lines. Missing or unlocatable evidence downgrades the claim.
- Reproduction is attempted only for `pass` claims with an enumerated template;
  v1 ships R001 only.
- Evidence levels are recomputed at grading: `REPRODUCED` requires a successful
  sandbox repro, `STATIC_CONFIRMED` requires rule confirmation or structured
  reviewer evidence, and otherwise the finding is `SUSPICIOUS` and folded.
  Refuted and unverifiable claims are retracted with a `RetractReason`.

## Determinism and models

- The default and evaluation model is `DeterministicFakeLLM`. It never reads
  the clock, randomness, or the network. CI runs only the fake and offline
  replay; no workflow step contacts a provider.
- `--llm replay --transcript FILE` replays a recorded transcript byte-for-byte.
  Replay validates purpose, template version, prompt digest, response digest,
  and an optional `--model-id`; a mismatch is an enumerated error and never
  falls back to the fake or another recording.
- `--llm real --model-id ID` uses the single OpenAI-compatible adapter behind
  `ProviderTransport`. The credential is read at call time from
  `TESTLENS_LLM_API_KEY` (overridable in code) and is never logged or stored.
  The endpoint defaults to `https://api.openai.com/v1` and can be overridden
  with `TESTLENS_LLM_BASE_URL`; HTTPS and certificate verification stay on.
- `--record-to FILE` writes a versioned transcript for later replay. Writes are
  atomic, an existing file is refused, and the file stores response text,
  digests, model identity, finish reason, attempts, and usage — never prompts,
  system messages, API keys, or headers.
- A real or replayed run reports `model_kind=real`; replay adds
  `replayed=true`. Both are excluded from deterministic baseline comparisons.
- Retries: at most 3 attempts with 1s/2s/4s backoff, only for timeouts, HTTP
  429, and HTTP 5xx. Auth and invalid-response failures are fatal. Model
  failures never become empty proposals or fake responses.
- Per-run budgets default to 200 calls and 500,000 tokens
  (`--max-calls`, `--max-tokens`); exceeding either raises
  `LLM_BUDGET_EXCEEDED`. Model/transport/budget failures exit with
  `EXIT_MODEL=4`.
- Evaluation uses fixed seeds, fixed proposals, and `double_run=true`; a report
  with double-run disabled is marked and excluded from evaluation.

```bash
# Replay a recorded transcript without network access.
testlens audit path/to/project \
  --llm replay --transcript tests/fixtures/llm/replay_transcript.json

# Manual real-model run; record it for later replay.
TESTLENS_LLM_API_KEY=... testlens tests path/to/project \
  --llm real --model-id gpt-4o-mini \
  --record-to artifacts/transcript.json

# Replay the recorded run.
testlens tests path/to/project \
  --llm replay --transcript artifacts/transcript.json
```

A real or replay evaluation writes under `eval/reports/real/` or
`eval/reports/replay/`, so it never overwrites the fake baseline. Do not commit
real transcripts; the only vendored transcript is the test fixture under
`tests/fixtures/llm/`.

## Evaluation baseline

The vendored fixtures ship inside the `evaluation` package:

- `evaluation/fixtures/fastapi_project`
- `evaluation/fixtures/flask_project`

Each is self-contained and offline; decorators use local stubs, so no framework
installation or network access is needed. `labels.json` records expected
defects, known false positives, and known false negatives by exact source
marker. `proposals.json` supplies fixed fake proposals.

`python -m evaluation` writes `report.json`, `calibration.json`, and
`summary.md` under the timestamped directory `eval/reports/<timestamp>/`
relative to the current working directory.
Reports include kept tests, discard-reason distribution, mean kill rate with
the assertion/exception/timeout breakdown, flaky rate, platform, enforcement
mode, audit precision, per-rule hits and false positives, evidence-level and
retract-reason distributions, and the equivalent-mutant caveat.

The calibrated mutation-kill threshold is **0.5**. Calibration ran `0.2 / 0.5 /
0.8` on both fixtures. Strong proposals measured `1.0`, weak proposals `0.0`,
and partial proposals `0.25` (FastAPI) and `0.33` (Flask); `0.2` keeps the
partial tests, while `0.5` and `0.8` keep only the strong test. The evidence is
in each run's `calibration.json`.

## Safety and isolation

The default backend uses `subprocess` with a timeout and an isolated work
directory. On POSIX it applies `RLIMIT_AS` and `RLIMIT_CPU`; on Windows the
timeout is enforced and `psutil` provides best-effort memory/CPU limits. Reports
state the backend, platform, and whether enforcement was `enforced` or
`best-effort`.

The opt-in container backend adds network and filesystem isolation on Linux;
`subprocess` does not claim either. **Production isolation still requires a
hardened container runtime and image.** The deterministic evaluation baseline
is Linux; the development environment is Windows/Git Bash.

CI (`.github/workflows/ci.yml`) runs the suite and the evaluation baseline on
Ubuntu for Python 3.10 and 3.12, and fails unless the report says
`platform=Linux` and `enforcement_mode=enforced`. The Linux `enforced` baseline
has not been run on the Windows development machine; local Windows reports
state `best-effort`.

## Known limitations

- Reproduction templates exist only for R001.
- The mutation operator set has 14 fixed operators; equivalent mutants are not
  detected, so observed kill rates are a lower bound.
- R005 has explicit v1 gaps: no `pathlib.Path.open`/`requests` detection, no
  `ExitStack` modeling, and `sqlite3.connect` under `with` is treated as
  managed. Cross-check refutations cover enumerated `try/finally: close()` and
  `ExitStack.enter_context` false positives.
- Test generation covers module-level route handlers only; class methods and
  nested functions are out of scope.
- No `pip install` is executed for the target project; dependencies must be
  pre-provisioned.
- The default `subprocess` backend inherits the parent environment. Do not run
  untrusted projects with secrets in environment variables; use the container
  backend or a hardened runtime for production.
- The container backend is Linux-only in 2A. It enforces limits but cannot
  report measured peak RSS/CPU without runtime support, so those fields are
  `None`.

## Development

```bash
python -m pytest
python -m pytest tests/test_evaluation.py
python -m evaluation --no-write
```

Reports and caches are written under `eval/reports/` and ignored by git.
