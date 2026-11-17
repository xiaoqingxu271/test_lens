# Changelog

## Unreleased

### Added

- Opt-in LLM-independent semantic audit claims (2C): `testlens audit
  PROJECT --independent` and `testlens eval --independent`.
- `audit/independent.py` sends one `llm_independent` request per module-level
  function and anchors each claim from a verbatim quote. The model supplies no
  file, line number, rule id, or verdict.
- Closed `ClaimKind` set: `MISSING_INPUT_VALIDATION`, `UNSAFE_SINK`,
  `SWALLOWED_ERROR`, `RESOURCE_LIFECYCLE`, `INCONSISTENT_STATE`.
- Deterministic dedup: span overlap with a rule candidate becomes
  `DUPLICATE_OF_RULE`; a repeated anchor becomes `DUPLICATE_CLAIM`.
- `IndependentClaim`, `RejectedIndependentClaim`, `IndependentTrace`,
  `IndependentReport`, and the additive `Finding.claim_kind` field.
- Independent findings are capped at `STATIC_CONFIRMED`, never `REPRODUCED`,
  and never folded as `SUSPICIOUS`; they still require structured cross-check
  evidence.
- The deterministic reviewer now returns `unverifiable` for semantic claims
  without a registered verdict instead of auto-passing them. The default fake
  proposes no independent claims.
- Evaluation reports rule and independent precision/recall separately;
  `independent_proposals.json` seeds fixed offline independent responses, and
  `--independent` requires fixtures with `llm_independent` labels. Rule-only
  metrics ignore those labels, so the default 5-finding baseline is unchanged;
  `--independent` reports 6 findings per packaged fixture.
- SQLite schema version 2 adds nullable `claim_kind` to `findings` and
  `retracted`, with an in-place migration from version 1.
- Offline record/replay coverage for `llm_independent` requests, including
  `MISSING_RESPONSE` when a recording is absent.
- `docs/v2-spec-2c.md` accepted 2C specification.

## 2.0.0 — 2026-11-16

### Added

- Opt-in Docker container backend for the sandbox (`--sandbox-backend
  container --sandbox-image IMAGE`), with `--network none`, read-only project
  and root filesystems, dropped capabilities, PID/memory/CPU limits, an
  allowlisted environment, and explicit no-fallback availability errors.
- `sandbox/container.py`, `sandbox/Dockerfile`, and container tests in
  `tests/test_container.py` / `tests/test_container_config.py`.
- `docs/v2-spec.md` accepted 2A container specification.
- Real-model and replay layer (2B): `--llm {fake,replay,real}`,
  `--transcript`, `--record-to`, `--model-id`, `--max-calls`, and
  `--max-tokens` on `tests`, `audit`, and `eval`.
- `llm/replay.py` versioned transcript loader and deterministic `ReplayLLM`;
  `llm/recording.py` atomic transcript writer; `llm/real.py` the single
  OpenAI-compatible adapter behind `ProviderTransport`; `llm/budget.py`
  per-run call/token budgets; `llm/errors.py` enumerated failures.
- Replay validates purpose, template version, prompt and response digests, and
  an optional expected model id; failures never fall back to the fake.
- Real-model retries: 3 attempts, 1s/2s/4s backoff, only timeouts, HTTP 429,
  and HTTP 5xx. `EXIT_MODEL=4` for model, transport, and budget failures.
- `GenerationTrace` and `TriageTrace` record `llm_calls`, `llm_retries`,
  `prompt_tokens`, `completion_tokens`, and `replayed`; `EvaluationTrace`
  aggregates them across fixtures.
- Offline replay tests and a vendored transcript fixture under
  `tests/fixtures/llm/`; `docs/v2-spec-2b.md` accepted 2B specification.

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
