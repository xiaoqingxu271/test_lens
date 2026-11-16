# Changelog

## 1.0.0 — 2026-11-16

First complete v1 release. A model only proposes candidates; sandboxed
execution, mutation scoring, deterministic rules, and an independent
cross-check decide what survives. Every rejection carries an enumerated reason.

### Test side

- Sandbox runner with subprocess timeout and isolated work directories; POSIX
  hard limits via `RLIMIT_AS` / `RLIMIT_CPU`, Windows best-effort limits via
  `psutil`.
- Mandatory double-run flaky detection; flaky tests are excluded from kept
  tests and pass-rate math.
- Fixed 14-operator AST mutator and target-local mutation scoring with
  assertion/exception/timeout breakdown.
- Deterministic target selection for module-level FastAPI/Flask route handlers.
- Sandbox-gated proposal validation and generation over the `LLMClient`
  interface.
- Test pipeline closure with isolated materialization and enumerated discard
  attribution.

### Audit side

- Deterministic rule scan: R001 mutable defaults, R002 local `None`
  dereference, R003 unreachable code after `return`/`raise`, R004 bare
  `except`, R005 resource opened without `with`.
- Line-anchored LLM triage that cannot move anchors or change rules.
- Independent false-positive cross-check with re-anchored evidence lines and
  enumerated context refutations.
- Sandbox reproduction for R001 and evidence grading into `REPRODUCED`,
  `STATIC_CONFIRMED`, or folded `SUSPICIOUS`; refuted and unverifiable claims
  are retracted with a `RetractReason`.

### Evaluation and interfaces

- Two offline vendored fixtures (FastAPI and Flask) shipped as `evaluation`
  package data, with marker-based labels and fixed fake proposals.
- Calibrated mutation-kill threshold of `0.5`, with recorded `0.2 / 0.5 / 0.8`
  evidence.
- CLI (`testlens tests|audit|eval`) and a versioned SQLite projection of test,
  audit, and evaluation reports.
- Contract tests guarding the frozen report fields and enum values.

### Constraints

- The deterministic fake is the only model client in v1; no real model or
  network call exists. Real-model runs must be labeled `ModelKind.REAL`.
- v1 uses subprocess isolation only and does not claim network or complete
  filesystem isolation. Production isolation requires containers.
- Equivalent mutants are not detected; killed-rate ceilings are a lower bound.
- Reproduction templates exist only for R001. `LLM_INDEPENDENT` findings are
  reserved for v2.
- The Linux `enforced` baseline is validated by CI; the Windows development
  machine reports `best-effort`.

### Verification

- Full suite: 94 tests passing, repeated three times without flakiness.
- Wheel and editable installs verified from outside the repository, including
  packaged fixtures and the `testlens` entry point.
