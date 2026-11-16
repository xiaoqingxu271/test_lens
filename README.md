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
  the clock, randomness, or the network.
- No real model client exists in v1. A future real-model run must be labeled
  separately in reports.
- Evaluation uses fixed seeds, fixed proposals, and `double_run=true`; a report
  with double-run disabled is marked and excluded from evaluation.

## Evaluation baseline

The vendored fixtures are:

- `eval/fixtures/fastapi_project`
- `eval/fixtures/flask_project`

Each is self-contained and offline; decorators use local stubs, so no framework
installation or network access is needed. `labels.json` records expected
defects, known false positives, and known false negatives by exact source
marker. `proposals.json` supplies fixed fake proposals.

`python -m evaluation` writes `report.json`, `calibration.json`, and
`summary.md` under the timestamped directory `eval/reports/<timestamp>/`.
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

v1 uses `subprocess` with a timeout and an isolated work directory. On POSIX it
applies `RLIMIT_AS` and `RLIMIT_CPU`; on Windows the timeout is enforced and
`psutil` provides best-effort memory/CPU limits. Reports state the platform and
whether enforcement was `enforced` or `best-effort`.

**v1 does not claim network or complete filesystem isolation. Production
isolation requires containers.** The deterministic evaluation baseline is
Linux; the development environment is Windows/Git Bash.

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

## Development

```bash
python -m pytest
python -m pytest tests/test_evaluation.py
python -m evaluation --no-write
```

Reports and caches are written under `eval/reports/` and ignored by git.
