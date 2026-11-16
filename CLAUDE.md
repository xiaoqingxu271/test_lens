# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

TestLens is an early v1 repo. The validation infrastructure (sandbox runner, double-run flaky detection, mutation operators and scoring), deterministic target selection, deterministic rule scan (R001–R005), the LLM interface with a deterministic fake, sandbox-gated candidate test generation, line-anchored audit triage, the false-positive cross-check, sandbox reproduction, evidence grading/`AuditReport` emission, the test-side pipeline closure, and the evaluation harness on vendored fixtures are implemented; the CLI, README, and SQLite metadata are not. This file is the single source of truth for the v1 spec; the confirmed design decisions at the end are normative. Continue in Build order; do not add a real model client or an evaluation run before triage and review exist.

Goal: for small Python web projects (FastAPI/Flask style with clear routes and function boundaries), an LLM only proposes candidates. Sandboxed execution, mutation scoring, and an independent false-positive review decide what survives; attribution explains every rejection.

## Tech stack

- Python 3.10+; standard library `ast` / `inspect` for static analysis; FastAPI/Flask decorator-style route extraction.
- pytest runs inside a sandbox subprocess against the target project.
- Sandbox: `subprocess` + timeout. POSIX uses `resource` hard limits (`RLIMIT_AS`, `RLIMIT_CPU`); Windows enforces timeout only and uses `psutil` (optional extra `sandbox`) for best-effort memory/CPU monitoring and limits. No ctypes Job Object. The deterministic evaluation baseline is Linux; reports state the platform and enforcement mode (`enforced` / `best-effort`).
- Mutation: self-built minimal AST mutator with a fixed, enumerable operator set; no external mutation service.
- Metadata and reports: SQLite.
- LLM behind an interface; deterministic fake is the default. Real-model runs are separately labeled.
- No LangChain; stages are explicit functions, each independently testable.

## Source layout

- `sandbox/` — `runner.py` (frozen execution report), `double_run.py` (flaky merge), `health.py` (generation gate: acceptance probe), `_pytest_plugin.py` (child-side test reporting and target-symbol coverage).
- `mutation/` — fixed AST operator set and mutation scoring.
- `project/` — deterministic Python file discovery, parsing, and enumerated skip reasons.
- `targets/` — AST route extraction and target-selection reports.
- `audit/` — rule scan, R001–R005 handlers, LLM triage, sandbox reproduction, and evidence grading; `RuleCandidate` and `TriageDraft` are intermediate stage outputs, not final `Finding`s.
- `llm/` — provider-neutral request/response contracts, the `LLMClient` protocol, and the deterministic fake.
- `generation/` — prompt construction, deterministic proposal validation, and `generate_tests`; emits candidates only and never executes or writes the target project.
- `review/` — false-positive cross-check: deterministic rule reviewer, independent reviewer interface and fake, context assembly, and evidence-anchor validation.
- `pipeline/` — test-side closure: isolated materialization, per-candidate double-run execution, target-local mutation scoring, enumerated discard attribution, and `TestReport`; never writes the target project.
- `evaluation/` — evaluation harness and metrics over the vendored fixtures; `eval/fixtures/` holds the self-contained FastAPI/Flask projects, labels, and fixed fake proposals; `eval/reports/` is generated output (gitignored).

## Build order (non-negotiable)

1. Sandbox executor (subprocess + timeout + resource limits + isolated workdir) and minimal AST mutator.
2. Deterministic target selection and rule scan.
3. LLM generation/triage behind an interface (deterministic fake by default).
4. Cross-check, sandbox reproduction, grading, test pipeline closure, evaluation harness.

No test-generation stage may be entered if the sandbox executor is unavailable or failing its own acceptance tests.

## Shared architecture

Both sides follow the same discipline: **candidate → verification → attribution → graded output**. Every kept item must be explainable by *why it passed*; every discarded item must carry an enumerated reason (never `unknown`, never silently kept).

### Test pipeline (stage order is load-bearing)

`select_targets` (static, no LLM; coverage only orders targets) → `generate_tests` → `execute_in_sandbox` (double-run for flakiness) → `mutate_and_score` (score only mutations in the target symbol) → `attribute_discard`.

A test is kept only if it passes in the sandbox **and** reaches the configurable mutation-kill threshold. Line coverage is a ranking signal only; it is never an acceptance criterion.

### Audit pipeline

`rule_scan` (deterministic, precise line numbers first) → `llm_triage` (line-anchored drafts only) → `cross_check` (independent adversarial review) → `sandbox_repro` (only for claims of reproducibility) → `grade_and_emit`.

LLM never owns line localization or rule matching; those stay deterministic. A failed cross-check downgrades or retracts — it never leaves a low-confidence finding in the output.

## Hard invariants

- Failed tests go to attribution only — no retries-to-pass, no skipping failed cases.
- Double-run disagreement → `flaky` list; excluded from kept and from pass-rate math.
- Findings at `SUSPICIOUS` are folded by default; `retracted` carries a `RetractReason`.
- A finding cannot be marked `REPRODUCED` unless the sandbox produced a minimal repro. If repro fails and static evidence is sufficient, the ceiling is `STATIC_CONFIRMED`.
- Deterministic rules decide file selection, line location, rule matching, and review verdicts.
- Concurrency/ordering effects must be considered for flakiness detection, but tests are not "fixed" by retrying.
- The runner return structure established in the first increment is frozen: later increments may add optional fields only.
- Double-run is on by default and mandatory for evaluation. A run with it disabled is marked `double_run=false` and excluded from evaluation reports.
- Mutation results are reported with the assertion/exception/timeout breakdown; an aggregate kill rate alone is not a valid report.
- A cross-check verdict without structured `evidence_lines` cannot support `STATIC_CONFIRMED`.

## V1 deterministic rule set

| Rule ID | Detects | V1 scope |
|---|---|---|
| `R001-MUTABLE-DEFAULT` | Mutable default argument (`def f(x=[])`) | Full |
| `R002-NONE-DEREF-NO-GUARD` | Local `None` dereference without a guard | Conservative: only explicit `x = None` followed by a direct dereference in the same function; no cross-function dataflow; prefer false negatives |
| `R003-UNREACHABLE-AFTER-RETURN` | Unreachable code after `return`/`raise` | Full |
| `R004-BARE-EXCEPT-SWALLOW` | Bare `except` / `except: pass` swallowing | Full |
| `R005-RESOURCE-NO-WITH` | File/connection opened without `with` | Full |

Rule IDs are used in reports and evaluation grouping. Adding a rule requires updating the evaluation labels and this list.

### Target selection (v1)

`targets/selection.py::select_targets(profile, coverage_rank=None)` is AST-only and never imports or executes the project.

- Scans module-level `def`/`async def` only; class methods and nested functions are out of scope in v1.
- Recognized decorator calls: `.get`, `.post`, `.put`, `.delete`, `.patch`, `.options`, `.head`, `.trace`, `.route`, `.api_route`; `.websocket` is recorded as `UNSUPPORTED_DECORATOR`.
- Path must be a literal string (positional, or `path`/`rule` keyword). `route`/`api_route` methods come from a literal list/tuple/set (or a single string), default to `GET`; dynamic/missing path or methods produce `SkippedRoute` with `DYNAMIC_PATH`, `DYNAMIC_METHODS`, or `MISSING_PATH`.
- `SelectedTarget.symbol_id` is `<relative POSIX path>::<function name>`; `line_start` is the `def` line and `line_end` the function end.
- Static order is `(relative_file, line_start, name)`. Optional `coverage_rank` is keyed by `symbol_id` and only reorders (descending rank, then static tie-break); it never removes a target and sets `order_reason=COVERAGE`.
- File discovery is shared with rule scan: excludes test directories/files, `.venv`, caches, VCS metadata, `migrations`/`alembic`, `conftest.py`, and `setup.py`. Syntax/decode/read failures become `SkippedFile` with `SYNTAX_ERROR`, `DECODE_ERROR`, `READ_ERROR`.
- v1 does not resolve FastAPI `APIRouter(prefix=...)`, Flask `url_prefix`, or dynamically constructed routes; the route path is the declared literal. Test generation is responsible for effective-path composition.

### Rule scan (v1)

`audit/rule_scan.py::scan_rules(profile, rules=ALL_RULES)` parses files (no execution) and emits `RuleCandidate` records; `rule_id`, exact line span, `snippet`/`snippet_hash`, and a stable `finding_id` are deterministic. `source` is `RULE`; `RULE_WITH_LLM_TRIAGE`/`LLM_INDEPENDENT` are not emitted here.

- R001: one candidate per function/lambda with mutable defaults. Detects list/dict/set literals, list/dict/set comprehensions, tuples containing a mutable default, and unshadowed calls to `list`/`dict`/`set`/`bytearray` (including `builtins.*`). A module-level binding of a constructor name suppresses that constructor; outer-function-local shadowing of nested defaults is not modeled.
- R002: same-function, conservative. Tracks simple `x = None`; flags direct `x.attr`, `x[...]`, `x(...)` only. Guards (`x is not None`, `x`, `assert ...`, `not x`, `and`/`or`, conditional expressions) are honored in the branch. State is cleared after `if`/`for`/`while`/`try`/`with`/`match` to prefer false negatives. Comprehensions, lambdas, nested functions, class bodies, and module-level code are not analyzed.
- R003: direct `return`/`raise` in a statement list; one candidate per outermost unreachable region; nested statements inside that region are not re-reported. `else`/`finally`/`except` blocks are separate statement lists and are not false positives.
- R004: every bare `except:`; one candidate per handler; the message distinguishes `pass`/`...` swallowing from a bare handler that does not narrow.
- R005: fixed resource call set: `builtins.open`, `io.open`, `codecs.open`, `gzip.open`, `bz2.open`, `lzma.open`, `tarfile.open`, `zipfile.ZipFile`, `tempfile.NamedTemporaryFile`, `tempfile.TemporaryFile`, `tempfile.SpooledTemporaryFile`, `sqlite3.connect`, `socket.socket`, `socket.create_connection`, `urllib.request.urlopen`. Calls in `with`/`async with` context expressions are exempt; calls in the body are not. Import aliases (`import x as y`, `from x import y as z`) and local/module/parameter shadowing are resolved; wildcard imports are not resolved.
- Known R005 limits in v1: no `try/finally: close()` analysis, no `ExitStack.enter_context`, no `pathlib.Path.open`/`requests` detection, and `sqlite3.connect` under `with` is treated as managed even though that context manager commits/rolls back rather than closing. These are explicit false-positive/false-negative classes for the cross-check and evaluation fixtures.
- `RuleScanTrace` records `files_scanned`, `files_skipped`, `candidates_found`, per-rule counts, `rules_run`, and elapsed time. `RuleCandidate` is not a final `Finding`: evidence levels, triage, cross-check, reproduction, and `AuditReport` remain later stages.

### LLM interface and generation (v1)

`llm/` is the only place model clients live. `LLMRequest` carries a deterministic `request_id`, purpose, prompt-template version, system/user prompt, seed, and temperature; `prompt_digest` is computed from template version + system + prompt. `LLMResponse` carries text, `model_id`, `model_kind` (`fake` / `real`), finish reason, usage, and a computed `response_digest`. `LLMClient` exposes only `model_kind`, `model_id`, and `complete(request)`. No real client or network call exists in v1; real-model runs must be labeled `ModelKind.REAL` in reports.

- `DeterministicFakeLLM` resolves a registered response by `request_id` first, then falls back to a deterministic default responder; it never reads the clock, randomness, or the network. The default generation responder is a stand-in whose proposals are expected to be filtered later by the sandbox, mutation scoring, and attribution.
- `generate_tests(profile, selection, llm, sandbox_health=None)` sends one request per `SelectedTarget` (`request_id = test_generation::<symbol_id>`) with template `generation-v1`, validates every proposal deterministically, and never writes project files or executes the project.
- Rejection reasons (`GenerationRejectReason`): `EMPTY_RESPONSE`, `NO_PROPOSAL`, `INVALID_JSON`, `INVALID_SHAPE`, `SOURCE_UNREADABLE`, `INVALID_TEST_PATH`, `FILE_EXISTS`, `SYNTAX_ERROR`, `NO_TEST_FUNCTION`, `TARGET_MISMATCH`, `DUPLICATE_TEST_ID`. Rejected proposals are reported, never silently dropped.
- An accepted candidate defines exactly one top-level test function matching `test_name`, uses a relative POSIX `test_*.py` / `*_test.py` path that does not collide with an existing file, parses with `ast`, and imports or references the target symbol. `test_id` is `<test_file>::<test_name>`. Target selection and ordering stay deterministic; the LLM never selects files or lines.
- `GeneratedTest` is a candidate, not a kept test: sandbox execution, double-run, mutation scoring, and attribution decide survival.
- Sandbox gate: `sandbox/health.py::check_sandbox_ready()` runs a strong and a weak test through the double-run executor in a temporary project. `SandboxHealth` records platform, enforcement mode, runner version, whether the strong test passed and entered the target symbol, whether both runs were present, and enumerated `SandboxHealthFailure` values. `generate_tests` raises `SandboxUnavailableError` unless the probe is ready.
- `GenerationTrace` records model kind/id, template version, targets considered, tests proposed/accepted/rejected, rejections by reason, elapsed time, the sandbox health result, and warnings (including model identity mismatches).

### LLM triage (v1)

`audit/triage.py::llm_triage(profile, rule_scan_report, llm)` sends one request per `RuleCandidate` (`request_id = rule_triage::<finding_id>`) using template `triage-v1`. The prompt contains the candidate fields, its enclosing function source, and up to two context lines before and after; it does not receive prior reasoning. Triage may only complete `trigger_path` and restate `claim`.

- File, rule id, and line span are copied from the deterministic candidate and validated for exact equality. A response that references another finding, changes the rule, or moves the anchor is rejected.
- Accepted drafts have `source=RULE_WITH_LLM_TRIAGE` and retain the candidate's `finding_id`, `rule_id`, file, and line span. Candidates whose triage is rejected remain in the rule scan report with `source=RULE`; they are not silently upgraded.
- Rejection reasons (`TriageRejectReason`): `EMPTY_RESPONSE`, `INVALID_JSON`, `INVALID_SHAPE`, `SOURCE_UNREADABLE`, `UNKNOWN_FINDING`, `RULE_MISMATCH`, `LINE_ANCHOR_MISMATCH`, `NO_TRIGGER_PATH`, `EMPTY_CLAIM`.
- `TriageDraft` carries `finding_id`, `rule_id`, `source`, `file`, `line_start`, `line_end`, `claim`, `trigger_path`, prompt/response digests, template version, and model identity.
- `TriageTrace` records model kind/id, template version, candidates considered, drafts accepted/rejected, rejections by reason, elapsed time, and warnings.
- Triage is not a final finding: cross-check, reproduction, and evidence grading remain later stages. `LLM_INDEPENDENT` is still not emitted.

### False-positive cross-check (v1)

`review/` is independent of `llm/`: `ClaimReviewer` is a separate interface returning structured `ReviewerVerdict` objects, and `DeterministicReviewer` has its own implementation and state. It does not import, subclass, or share state with the triage fake.

- `cross_check(profile, rule_report, subjects, reviewer)` accepts `CrossCheckSubject`s; `subjects_from_drafts` converts `TriageDraft`s (rule id, file, span, claim, trigger path). Triage reasoning text is never part of the request.
- Rule-backed claims go to `RuleReviewer` first: it re-runs the deterministic rule on the current source and passes only if the same span still matches. Its verdict is authoritative and free-text review cannot override it. Enumerated context refutations in v1: R005 `try/finally: close()` and `ExitStack.enter_context` produce `REFUTE` + `REFUTED_BY_CONTEXT` with anchored evidence.
- Semantic claims (`rule_id=None`, the v2 path) go to the independent reviewer through `ReviewRequest`, which carries the enclosing function, imports, routes, callers, type hints, existing guards, and other rule hits in the file.
- `pass` requires at least one `supports` evidence line; `refute` requires at least one `refutes` line. Every evidence line is re-anchored against the current source: the file must stay inside the project, the span must exist, and `snippet_hash` must match. Missing evidence → `UNVERIFIABLE_CLAIM`; unlocatable or mismatched evidence → `NO_LINE_LOCATION`; either way the verdict is downgraded to `unverifiable`.
- `refute` always carries a `RetractReason` (default `REFUTED_BY_CONTEXT`); `unverifiable` defaults to `UNVERIFIABLE_CLAIM`.
- `CrossCheckResult` follows decision 3. `CrossCheckTrace` records subjects considered, verdict counts, rule checks, model reviews, reviewer ids, elapsed time, and warnings.
- Cross-check is not the final output: sandbox reproduction and evidence grading remain later stages.

### Sandbox reproduction (v1)

`audit/repro.py::run_repro(profile, rule_report, cross_check_report, ...)` attempts reproduction only for claims whose cross-check verdict is `pass` and whose rule has an enumerated template. v1 registers R001 only.

- R001 template: locate the containing module-level function; skip async functions and functions with required parameters (`UNSUPPORTED_SIGNATURE`); derive a probe from the mutable default AST (list/dict/set/bytearray literals and comprehensions, builtins constructors, tuples containing a mutable element); generate a minimal pytest test that mutates the first call's default-backed object and asserts the mutation is visible on a second call.
- The script is written to an isolated artifact directory, never the project; it runs through the normal double-run executor with the project root as cwd.
- Outcome mapping: `passed` → reproduced; assertion failure → `CLAIM_NOT_OBSERVED`; timeout → `TIMEOUT`; pass/fail disagreement → `FLAKY`; import/collection/runner failure → `SANDBOX_ERROR`.
- Skip reasons: `NOT_PASSED`, `UNSUPPORTED_RULE`, `UNSUPPORTED_SIGNATURE`, `TEMPLATE_BUILD_FAILED`. Rules without a template can never exceed `STATIC_CONFIRMED`.
- `ReproArtifact` records script source/path, exit code, timeout, elapsed time, stdout/stderr digests, platform, enforcement mode, the double-run flag, `reproduced`, and failure reason.

### Evidence grading and audit report (v1)

`audit/grade.py::grade_and_emit(rule_report, triage_report, cross_check_report, repro_report)` recomputes evidence levels from final evidence and emits `AuditReport`.

- `refute` → `Retracted` with the result's `RetractReason` (default `REFUTED_BY_CONTEXT`).
- `unverifiable` → `Retracted` (default `UNVERIFIABLE_CLAIM`).
- `pass` plus a `reproduced=true` artifact → `REPRODUCED`.
- `pass` plus rule confirmation or reviewer `evidence_lines` → `STATIC_CONFIRMED`.
- `pass` without either → `SUSPICIOUS`, folded into `folded_suspicious` by default.
- A failed repro never lowers static evidence below `STATIC_CONFIRMED`; only a valid sandbox repro can upgrade to `REPRODUCED`.
- Exactly one cross-check result is required per rule candidate; missing, duplicate, or unexpected results/artifacts raise.
- `Finding` carries file, line span, rule id, `llm_finding_id` (only when triage completed the trigger path), source, title, evidence level, claim, trigger path, and an optional repro artifact. `GradingTrace` records counts and evidence/retract distributions.
- `subjects_from_candidates` ensures triage-rejected candidates are still cross-checked and graded as `RULE` findings.

### Test pipeline closure (v1)

`pipeline/run.py::run_test_pipeline(profile, selection, generation, ...)` closes the test side: materialize → execute (double-run) → mutate and score → attribute discard → `TestReport`.

- Materialization copies the project and every accepted candidate into an isolated work directory; the target project is never written to. A traversal path, an existing file, a duplicate test id, or two candidates sharing one test file raises `PipelineError`.
- Execution runs one candidate per subprocess; double-run uses two subprocesses per candidate. A report whose test ids do not match the candidate raises.
- Mutation scoring runs only for candidates that passed both runs and entered the target symbol. Mutants are target-local and use the fixed operator set and seed.
- Attribution order: `FLAKY` (also in `flaky`), `ENVIRONMENT_MISSING` (import/collection/runner failure, skipped), `RUNTIME_ERROR_ON_HAPPY` (other non-pass), `TARGET_MISMATCH` (target never entered), `ASSERTION_TOO_WEAK` (`mutations_run == 0` or kill rate below the threshold), else kept. Failed tests are never retried.
- `TestPipelineTrace` records stage counts, the discard distribution, `double_run`, threshold, operator ids, seed, platform, enforcement mode, and warnings. A report with `double_run=false` is marked and excluded from evaluation.
- The default threshold is `0.5`; calibration belongs to the evaluation harness.

## Evaluation baseline

- Two vendored minimal fixture projects (one FastAPI, one Flask), with pinned dependencies (`eval/requirements.txt`), offline execution, known defects, and known false positives. Decorators use local stubs so no framework install or network is needed. Real OSS projects are optional, pinned by commit, and are not the deterministic baseline.
- Labels are marker-based (`labels.json`): each expected defect, false positive, and false negative names an exact source marker, resolved to a line at evaluation time.
- `proposals.json` supplies fixed deterministic-fake proposals per target; the fake is the default and model identity is recorded in the report.
- One command: `python -m evaluation` writes timestamped `report.json`, `calibration.json`, and `summary.md` under `eval/reports/`, with fixed seeds, `double_run=true`, and the platform/enforcement mode.
- Evaluation reports include kept count, `DiscardReason` distribution, mean kill rate with the assertion/exception/timeout breakdown, flaky rate, platform, enforcement mode, and the equivalent-mutant caveat; the audit side reports precision, per-rule hits/misses, evidence-level and retract-reason distributions.
- Calibrated mutation-kill threshold: `0.5`. Calibration ran `0.2 / 0.5 / 0.8` over both fixtures. Strong proposals measured kill rate `1.0`; weak proposals `0.0`; partial proposals `0.25` (FastAPI) and `0.33` (Flask). At `0.2` the partial tests are kept; at `0.5` and `0.8` only the strong test survives, so `0.5` separates weak from strong with margin on both fixtures. The evidence is in `calibration.json` and `summary.md` of each run.

## Code conventions

- Enums everywhere; no bare strings for reasons, evidence levels, statuses.
- Pydantic models at every stage boundary; no raw dicts passed between stages.
- Every stage emits trace data: elapsed time, candidate count, filtered count — so you can tell which stage dropped valid output.
- Mutator operator set is fixed and enumerable. Adding an operator requires updating the evaluation suite in the same change.
- The LLM is accessed through an interface. Default and evaluation mode use a deterministic fake; real-model runs must be labeled separately in reports.
- Evaluation is one command writing timestamped results under `eval/reports/`, with fixed seeds. Report kept counts, discard-reason distribution, mean kill rate, flaky rate; audit reports precision and evidence-level/retract-reason distributions. "How many tests/findings were produced" is not a success metric.

## Contract reference (exact names)

Discard reasons: `SYNTAX_ERROR`, `ENVIRONMENT_MISSING`, `RUNTIME_ERROR_ON_HAPPY`, `ASSERTION_TOO_WEAK`, `TARGET_MISMATCH`, `FLAKY`.

Evidence levels: `REPRODUCED`, `STATIC_CONFIRMED`, `SUSPICIOUS`.

Retract reasons: `REFUTED_BY_CONTEXT`, `NO_LINE_LOCATION`, `UNVERIFIABLE_CLAIM`, `DUPLICATE`.

Reports are `TestReport` (`kept`, `discarded`, `flaky`, `trace`) and `AuditReport` (`findings`, `folded_suspicious`, `retracted`, `trace`). `KeptTest` carries `target_symbol`, `test_file`, `test_name`, `execution_status="passed"`, `mutation_kill_rate`, `mutations_run`, `mutations_killed`. Each `Finding` carries `file`, `line_start`, `line_end`, `rule_id` or `llm_finding_id`, `source`, `title`, `evidence_level`, and an optional `repro_artifact`. In v1, `source` is `RULE` or `RULE_WITH_LLM_TRIAGE`; `llm_finding_id` identifies a rule candidate whose trigger path was completed by LLM triage, never an LLM-independent discovery. `LLM_INDEPENDENT` is reserved for v2. `Retracted` carries the finding reference and a `RetractReason`.

## Environment note

Development happens on Windows (Git Bash). `resource` is POSIX-only. POSIX enforces `RLIMIT_AS`/`RLIMIT_CPU`; Windows enforces the subprocess timeout and uses `psutil` (optional extra `sandbox`) for best-effort memory/CPU limits. Do not introduce ctypes Job Object code. The deterministic evaluation baseline is Linux; every report states the platform and whether limits were `enforced` or `best-effort`.

## Commands

- Install dev dependencies: `python -m pip install -e ".[dev]"`
- Run the suite: `python -m pytest`
- Run one file: `python -m pytest tests/test_mutation_score.py`
- Run target selection or rule scan: `python -m pytest tests/test_target_selection.py`, `python -m pytest tests/test_rule_scan.py`
- Run generation: `python -m pytest tests/test_generation.py`; sandbox gate: `python -m pytest tests/test_sandbox_health.py`
- Run audit triage: `python -m pytest tests/test_triage.py`; cross-check: `python -m pytest tests/test_cross_check.py`
- Run reproduction/grading: `python -m pytest tests/test_repro.py`, `python -m pytest tests/test_grade.py`
- Run test pipeline: `python -m pytest tests/test_pipeline.py`
- Run evaluation tests: `python -m pytest tests/test_evaluation.py`
- Run the evaluation baseline: `python -m evaluation` (writes under `eval/reports/`); `python -m evaluation --no-write` prints metrics only
- Run one test: `python -m pytest tests/test_runner.py::test_runner_strong_passes_and_covers_target`

Tests invoke pytest inside a sandbox subprocess against `tests/fixtures/sample_project` and `tests/fixtures/pipeline_project`; fixture tests are excluded from the outer collection. The README is still pending and must state that production isolation uses containers even though v1 uses `subprocess` + resource limits.

## Confirmed design decisions (v1)

These ten decisions are normative. Where they add detail, they override the summaries above.

### 1. Double-run and flakiness

- Every candidate test runs twice in independent subprocesses, not only candidates that passed the first run.
- The verdict compares pass/fail only. The two failure categories (exception type and message digest) are written to trace for diagnosis and do not affect the verdict.
- One pass and one failure → `FLAKY`: the test appears in both `discarded` (reason `FLAKY`) and the `flaky` list, and is excluded from `kept` and from the pass-rate denominator.
- Two failures of the same category → normal failure attribution, not flaky.
- Double-run is a config switch, on by default and mandatory for evaluation. When off, trace records `double_run=false` and the run must not enter an evaluation report.

### 2. Mutation scoring

- Denominator = `mutations_run`: mutants successfully applied and compiled. Numerator = mutants on which the test failed in at least one run.
- Mutants are generated only inside the target symbol. The set is deterministic (fixed seed, fixed operator order) and shared by all candidate tests for that symbol.
- Do not filter the denominator by lines the test actually reached.
- A timeout counts as killed, but `killed_by_assertion`, `killed_by_exception`, and `killed_by_timeout` are counted separately in trace and evaluation reports. An aggregate-only kill rate is not acceptable.
- Equivalent mutants are not detected in v1. Evaluation reports must state that the observed kill-rate ceiling is depressed by equivalent mutants.

`MutationScoreReport`:
- `target_symbol: str`
- `test_id: str`
- `operator_set_version: str`
- `seed: int`
- `mutants_total: int` — generated
- `mutations_run: int` — applied and compiled
- `mutations_killed: int`
- `kill_rate: float`
- `killed_by_assertion: int`
- `killed_by_exception: int`
- `killed_by_timeout: int`
- `survived: int`
- `mutant_ids: list[str]`
- `equivalent_mutants_undetected: bool`
- `mutant_results: list[MutantResult]` — optional per-mutant detail
- `trace: MutationTrace`

`MutationTrace`:
- `started_at: str`
- `elapsed_ms: int`
- `operator_ids: list[str]`
- `mutant_generation_digest: str`
- `killed_by_assertion: int`
- `killed_by_exception: int`
- `killed_by_timeout: int`
- `warnings: list[str]`

`MutantResult`:
- `mutant_id: str`
- `operator_id: str`
- `file: str`
- `line: int`
- `status: "killed_assertion" | "killed_exception" | "killed_timeout" | "survived" | "invalid"`
- `killed_by: Optional[str]`

### 3. False-positive cross-check

- Cross-check receives the draft's file/line span/claim/trigger path/rule id, the enclosing function, file-level context, callers/routes, type hints, existing guards, and other rule hits in the file. It does not receive triage's reasoning text.
- Rules run first and their verdict is authoritative when a rule fires. Semantic claims not covered by a rule go to an independent reviewer.
- Independence means a separate stage, interface, and instance. In fake mode the reviewer fake must not reuse the triage fake's implementation or state.
- The reviewer may pass a claim not covered by a rule and thereby support `STATIC_CONFIRMED`, but it must emit a structured evidence chain. Free-text-only verdicts are invalid.
- `evidence_lines` is required for `pass` and `refute` verdicts. If it is missing or its lines cannot be located, the finding can only be downgraded or retracted.

`CrossCheckResult`:
- `finding_id: str`
- `claim_hash: str`
- `verdict: "pass" | "refute" | "unverifiable"`
- `evidence_lines: list[EvidenceLine]`
- `retract_reason: Optional[RetractReason]`
- `checked_rule_ids: list[str]`
- `reviewer_kind: "rule" | "model" | "fake"`
- `reviewer_id: str`
- `source_context_hash: str`
- `elapsed_ms: int`

`EvidenceLine`:
- `file: str`
- `line_start: int`
- `line_end: int`
- `evidence_kind: "guard" | "unreachable" | "type_constraint" | "duplicate" | "reproduction" | "rule" | "data_flow"`
- `role: "supports" | "refutes"`
- `explanation: str` — how this line supports or refutes the claim
- `snippet_hash: str`
- `snippet: Optional[str]`

A `pass` verdict requires at least one `supports` evidence line; a `refute` verdict requires at least one `refutes` line. `unverifiable` may have none, but if the finding leaves the main output it must carry a `retract_reason`.

### 4. Evidence grading

- Levels are recomputed at `grade_and_emit` from final evidence; they are not manually toggled during the pipeline.
- Downgrade requires new evidence. Mere reviewer disagreement without concrete counter-evidence cannot downgrade a finding.
- `REPRODUCED` → `STATIC_CONFIRMED` is allowed when the repro artifact is shown not to support the claim, and static evidence remains sufficient.
- Upgrade is evidence-bound: only a valid sandbox repro can produce `REPRODUCED`; review approval alone cannot. `STATIC_CONFIRMED` requires either rule confirmation or review with `evidence_lines`.

### 5. Executor return structure (frozen)

The first increment must return the full structure below. Later increments may add optional fields, but must not rename or remove existing fields.

`ExecutionReport`:
- `run_id: str`
- `started_at: str`
- `elapsed_ms: int`
- `project_profile_id: str`
- `sandbox_config: SandboxConfig`
- `runs: list[TestRunResult]` — one per double-run execution; length 1 when double-run is off
- `tests: list[TestResult]` — merged per-test view
- `flaky: list[FlakyRecord]`
- `resource_summary: ResourceSummary`
- `timeout_summary: TimeoutSummary`
- `trace: ExecutionTrace`
- `runner_errors: list[RunnerError]`

`SandboxConfig`:
- `timeout_seconds: float`
- `memory_limit_mb: Optional[int]`
- `cpu_limit_seconds: Optional[int]`
- `double_run: bool`
- `enforcement_mode: "enforced" | "best-effort"`
- `platform: str`
- `coverage_source: "coverage.py" | "sys.settrace"`

`TestRunResult`:
- `run_index: int`
- `started_at: str`
- `elapsed_ms: int`
- `exit_code: int`
- `timed_out: bool`
- `tests: list[TestOutcome]`
- `resource_usage: ResourceUsage`
- `stdout_artifact: Optional[str]`
- `stderr_artifact: Optional[str]`

`TestOutcome`:
- `test_id: str` — stable `file::name`
- `test_file: str`
- `test_name: str`
- `status: "passed" | "failed" | "error" | "timeout" | "skipped"`
- `failure_kind: "none" | "assertion" | "exception" | "import_error" | "collection_error" | "timeout" | "runner_error"`
- `exception_type: Optional[str]`
- `message_digest: Optional[str]`
- `duration_ms: int`
- `covered_target_symbols: list[SymbolCoverage]`
- `timeout: TimeoutInfo`
- `resource_usage: ResourceUsage`
- `stdout_artifact: Optional[str]`
- `stderr_artifact: Optional[str]`

`TestResult` (merged):
- `test_id: str`
- `test_file: str`
- `test_name: str`
- `target_symbol: Optional[str]`
- `final_status: "passed" | "failed" | "error" | "timeout" | "skipped" | "flaky"`
- `runs: list[TestOutcome]`
- `flaky: bool`
- `failure_kinds: list[str]`
- `covered_target_symbols_union: list[str]`
- `timeout: TimeoutInfo`
- `resource_usage: ResourceUsage`

`SymbolCoverage`:
- `symbol_id: str`
- `file: str`
- `line_start: int`
- `line_end: int`
- `entry_hit: bool`
- `executed_lines: list[int]`
- `coverage_source: "coverage.py" | "sys.settrace"`

`FlakyRecord`:
- `test_id: str`
- `test_file: str`
- `test_name: str`
- `run_statuses: list[str]`
- `consistent: bool`
- `failure_details_by_run: list[FailureDetail]`

`FailureDetail`:
- `run_index: int`
- `failure_kind: str`
- `exception_type: Optional[str]`
- `message_digest: Optional[str]`

`TimeoutInfo`:
- `timed_out: bool`
- `timeout_seconds: float`
- `phase: "collection" | "execution" | "teardown"`
- `process_tree_killed: bool`

`ResourceUsage`:
- `wall_time_ms: int`
- `cpu_time_seconds: Optional[float]`
- `peak_rss_mb: Optional[float]`
- `limits_hit: list[str]`
- `enforcement_mode: "enforced" | "best-effort"`

`ResourceSummary`:
- `peak_rss_mb: Optional[float]`
- `cpu_time_seconds: Optional[float]`
- `wall_time_ms: int`
- `limits_hit: list[str]`
- `enforcement_mode: "enforced" | "best-effort"`
- `platform: str`

`TimeoutSummary`:
- `any_timeout: bool`
- `timed_out_test_ids: list[str]`
- `timeout_seconds: float`

`ExecutionTrace`:
- `double_run: bool`
- `runner_version: str`
- `python_version: str`
- `cwd: str`
- `env_digest: str`
- `warnings: list[str]`

`RunnerError`:
- `stage: "collection" | "execution" | "teardown"`
- `message: str`
- `fatal: bool`

Target coverage uses `coverage.py` (line-level) or `sys.settrace`; no self-written AST instrumentation. The chosen source is recorded in `coverage_source`.

### 6. LLM triage boundary

- v1 uses A: the LLM works only within `rule_scan` candidates and does not independently produce findings.
- A v1 finding's `source` is `RULE` or `RULE_WITH_LLM_TRIAGE`. `LLM_INDEPENDENT` is reserved for v2 and is not emitted in v1.
- `llm_finding_id` identifies a rule candidate whose trigger path was completed by LLM triage. It does not mean an independent LLM discovery.
- v2 may open B; its evaluation baseline must be defined separately.

### 7. V1 rule set

- `R001-MUTABLE-DEFAULT`: mutable default argument (`def f(x=[])`).
- `R002-NONE-DEREF-NO-GUARD`: local `None` dereference without a guard. Detect only an explicit `x = None` followed by a direct dereference in the same function; no cross-function dataflow. Prefer false negatives over false positives.
- `R003-UNREACHABLE-AFTER-RETURN`: unreachable code after `return`/`raise`.
- `R004-BARE-EXCEPT-SWALLOW`: bare `except` / `except: pass` swallowing.
- `R005-RESOURCE-NO-WITH`: file/connection opened without `with`.
- Rule IDs are used in reports and evaluation grouping. Adding a rule requires updating the evaluation labels and this list.

### 8. Evaluation baseline

- Two vendored minimal fixture projects (one FastAPI, one Flask), with pinned dependencies, offline execution, known defects, and known false positives. Real OSS projects are optional, pinned by commit, and are not the deterministic baseline.
- Initial mutation-kill threshold is `0.5`. Calibrate on the fixture evaluation, then record the calibrated value and its evidence in this file and the README.
- Evaluation reports include kept count, `DiscardReason` distribution, mean kill rate with the assertion/exception/timeout breakdown, flaky rate, platform, enforcement mode, and the equivalent-mutant caveat.
- One command, fixed seeds, timestamped output under `eval/reports/`.

### 9. Sandbox granularity and dependencies

- One subprocess per candidate test; double-run uses two subprocesses.
- Dependencies are pre-provisioned; v1 does not run `pip install`.
- v1 does not claim network or full filesystem isolation. The README must state that production requires containers.
- Entry points are a Python API plus a CLI.

### 10. Platform resource limits

- POSIX: `resource` hard limits (`RLIMIT_AS`, `RLIMIT_CPU`).
- Windows: subprocess timeout is enforced; `psutil` (optional extra `sandbox`) provides best-effort memory/CPU monitoring and limits. No ctypes Job Object code.
- The evaluation baseline is Linux. Reports state the platform and `enforced`/`best-effort`.
