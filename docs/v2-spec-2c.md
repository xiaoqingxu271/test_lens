# TestLens v2 specification — 2C LLM-independent audit findings

**Status: ACCEPTED 2026-11-17.** This document records the accepted design for
increment 2C; `CLAUDE.md` carries the normative summary.

This document specifies increment 2C: opening path B from v1 decision 6 so the
model may propose *semantic* audit claims that are not tied to R001–R005. It
does not change the test pipeline, the rule set, the mutation operators, or the
2B provider layer.

## 1. Goal

Add a third audit source, `LLM_INDEPENDENT`, while preserving every existing
guarantee:

- the model only proposes; it never owns file selection, line localization,
  deduplication, review verdicts, or evidence levels;
- claims are anchored deterministically against the current source;
- every kept finding is explainable by structured evidence, and every rejected
  claim carries an enumerated reason;
- the deterministic fake remains the default, and the fake evaluation baseline
  is unchanged unless independent labels and proposals are explicitly added;
- model, transport, and budget failures stay fatal and never become empty
  findings;
- CI makes no provider request.

## 2. Non-goals for 2C

- New deterministic rules or changes to R001–R005.
- Cross-function dataflow, taint analysis, or runtime tracing to validate a
  semantic claim.
- A generic reproduction template. Semantic claims cannot reach `REPRODUCED` in
  2C.
- Model-chosen files, line numbers, rule ids, or review verdicts.
- Streaming, multi-provider routing, or `LLM_INDEPENDENT` inside test
  generation.
- Turning independent scanning on by default.

## 3. Contract additions

All additions are optional or new records; no existing field is renamed or
removed.

```python
class LLMPurpose(str, Enum):
    TEST_GENERATION = "test_generation"
    RULE_TRIAGE = "rule_triage"
    LLM_INDEPENDENT = "llm_independent"   # new


class ClaimKind(str, Enum):
    MISSING_INPUT_VALIDATION = "MISSING_INPUT_VALIDATION"
    UNSAFE_SINK = "UNSAFE_SINK"
    SWALLOWED_ERROR = "SWALLOWED_ERROR"
    RESOURCE_LIFECYCLE = "RESOURCE_LIFECYCLE"
    INCONSISTENT_STATE = "INCONSISTENT_STATE"
```

New stage-boundary models in `audit/models.py`:

`IndependentClaim`
- `finding_id: str` — deterministic, computed by the stage
- `claim_kind: ClaimKind`
- `file: str` — relative POSIX path
- `line_start: int`, `line_end: int` — resolved from the quote, never supplied
  by the model
- `symbol_id: str` — `<relative POSIX path>::<function name>`
- `claim: str`
- `trigger_path: list[str]`
- `quote: str` — verbatim source span
- `snippet_hash: str`
- `prompt_digest`, `response_digest`, `prompt_template_version`
- `model_kind: ModelKind`, `model_id: str`

`RejectedIndependentClaim`
- `file: str`, `line_start: int | None`, `line_end: int | None`
- `symbol_id: str | None`
- `reason: IndependentRejectReason`
- `detail: str`
- `prompt_digest`, `response_digest`, `model_kind`, `model_id`

`IndependentRejectReason`
- `SOURCE_UNREADABLE`, `EMPTY_RESPONSE`, `INVALID_JSON`, `INVALID_SHAPE`,
  `UNSUPPORTED_CLAIM_KIND`, `EMPTY_CLAIM`, `EMPTY_QUOTE`, `QUOTE_TOO_LONG`,
  `UNLOCATABLE_QUOTE`, `AMBIGUOUS_QUOTE`, `CLAIM_OUTSIDE_FUNCTION`,
  `DUPLICATE_OF_RULE`, `DUPLICATE_CLAIM`

`IndependentTrace`
- `started_at`, `elapsed_ms`
- `files_scanned`, `functions_considered`, `requests_sent`
- `claims_received`, `claims_accepted`, `claims_rejected`
- `rejected_by_reason: dict[IndependentRejectReason, int]`
- `llm_model_kind`, `llm_model_id`, `prompt_template_version`
- `llm_calls`, `llm_retries`, `prompt_tokens`, `completion_tokens`, `replayed`
- `warnings: list[str]`

`IndependentReport`
- `claims: list[IndependentClaim]`
- `rejected: list[RejectedIndependentClaim]`
- `trace: IndependentTrace`

Additive report fields:

- `Finding.claim_kind: ClaimKind | None = None` — set only for
  `LLM_INDEPENDENT`.
- `GradingTrace.independent_candidates_considered: int = 0`
- `GradingTrace.independent_findings_emitted: int = 0`
- `GradingTrace.independent_retracted: int = 0`

## 4. Stage order and scope

Audit pipeline becomes:

```
rule_scan (deterministic)
  → independent_scan (LLM, opt-in)
  → llm_triage (rule candidates only)
  → cross_check (rule subjects + independent subjects)
  → sandbox_repro (rule subjects only)
  → grade_and_emit (rule + independent)
```

`audit/independent.py::scan_independent(profile, rule_report, llm)` is the new
stage.

- Scans module-level `def` / `async def` only, matching target selection.
  Class methods, nested functions, and module-level statements are out of
  scope.
- One request per function, in deterministic `(relative_file, line_start,
  name)` order. `request_id = llm_independent::<relative_file>::<name>`.
- Template version `independent-v1`. The prompt contains only deterministic
  context: file path, full function source and span, module imports, decorators
  and route literals, type hints, and rule candidates already found inside the
  function. It never contains triage or reviewer reasoning.
- Files that cannot be read are skipped with `SOURCE_UNREADABLE` and no model
  call. The rule scan's `files_skipped` stay authoritative for file-level
  failures.
- There is no extra function cap. The 2B budget (200 calls / 500,000 tokens)
  is the limit; exhaustion is fatal (`LLM_BUDGET_EXCEEDED`), not a partial
  result.
- A function may yield zero claims; that is a clean result, not a rejection.

## 5. Response shape and deterministic anchoring

The model returns one JSON object:

```json
{
  "claims": [
    {
      "claim_kind": "MISSING_INPUT_VALIDATION",
      "quote": "    limit = int(request.args.get('limit'))",
      "claim": "The limit is converted without validating attacker-controlled input.",
      "trigger_path": ["route handler", "query parameter", "int conversion"]
    }
  ]
}
```

Rules:

- `claim_kind` must be one of the closed set. Unknown kinds are rejected with
  `UNSUPPORTED_CLAIM_KIND`; the set is not extended at runtime.
- `quote` must be a non-empty verbatim substring of the requested function,
  at most 400 characters, with CRLF normalized to LF. It may span lines.
- The stage searches for `quote` only inside the function's source span.
  Exactly one occurrence is required:
  - zero → `UNLOCATABLE_QUOTE`
  - more than one → `AMBIGUOUS_QUOTE`
  - a resolved span that crosses the function boundary →
    `CLAIM_OUTSIDE_FUNCTION` (defensive; normally impossible)
- The model never supplies `line_start`, `line_end`, `file`, `rule_id`, or a
  `finding_id`; those are computed.
- `finding_id` is:
  `"indep::" + sha256(relative_file \x1f symbol_id \x1f claim_kind \x1f
  line_start \x1f line_end \x1f snippet_hash)[:16]`.
  It is stable across runs and independent of the claim wording.
- `snippet_hash` is the shared 16-hex source hash used by review.
- `title` is deterministic: `"<claim_kind>: <function name>"`.
- Claims are sorted by `(file, line_start, line_end, claim_kind, finding_id)`
  before deduplication and output.

Claim kinds:

| Kind | Claim shape |
|---|---|
| `MISSING_INPUT_VALIDATION` | A request value reaches a conversion, lookup, or branch without validation. |
| `UNSAFE_SINK` | Attacker-influenced data reaches a capability sink (eval/exec, filesystem path, shell, raw SQL). |
| `SWALLOWED_ERROR` | An error path discards failure and continues with invalid state. |
| `RESOURCE_LIFECYCLE` | A resource is used past its scope, closed conditionally, or leaked on an error path. |
| `INCONSISTENT_STATE` | Mutable module/application state is assumed per-request and can leak across calls. |

## 6. Deduplication

Two deterministic rules apply before claims enter cross-check:

1. **Rule overlap** — if a claim's `[line_start, line_end]` intersects any rule
   candidate in the same file (inclusive), the claim is rejected with
   `DUPLICATE_OF_RULE`. Rule findings win; independent findings are never
   merged into or used to upgrade a rule finding.
2. **Claim duplicate** — if two accepted claims resolve to the same
   `finding_id`, the first in deterministic sort order wins; the rest are
   rejected with `DUPLICATE_CLAIM`.

No fuzzy matching, embedding similarity, or claim-text comparison is used.

## 7. Cross-check

`subjects_from_independent_claims(report)` builds one
`CrossCheckSubject` per accepted claim with `rule_id=None`,
`source=LLM_INDEPENDENT`, and the resolved span/claim/trigger path.

- The existing semantic path in `review/` is used; rule-backed behavior is
  unchanged.
- A `pass` requires at least one `supports` evidence line; a `refute` requires
  at least one `refutes` line. Every evidence line is re-anchored and its
  `snippet_hash` verified, exactly as in v1.
- Missing or unlocatable evidence downgrades to `unverifiable` with
  `UNVERIFIABLE_CLAIM` or `NO_LINE_LOCATION`; the finding is retracted.
- The independent reviewer remains a separate interface, instance, and state
  from the triage fake. Triage is never invoked for semantic claims.
- `DeterministicReviewer` changes one default: a semantic claim
  (`rule_id=None`) with no registered verdict returns `unverifiable` with
  `UNVERIFIABLE_CLAIM` instead of the current automatic `pass`. Rule-backed
  defaults are unchanged. This prevents the fake reviewer from confirming every
  semantic claim and keeps the precision gate real.
- Evaluation and tests register semantic verdicts explicitly.

## 8. Evidence grading

`grade_and_emit` gains an optional `independent_report` argument:

```python
grade_and_emit(
    rule_report,
    triage_report,
    cross_check_report,
    repro_report,
    independent_report=None,
)
```

- Exactly one cross-check result is required for every accepted independent
  claim, in addition to the existing one-per-rule-candidate invariant. Missing,
  duplicate, or unexpected results raise.
- `refute` → `Retracted` with the result's `RetractReason`.
- `unverifiable` → `Retracted` (default `UNVERIFIABLE_CLAIM`).
- `pass` with structured supports evidence → `STATIC_CONFIRMED`.
- `pass` with no supports evidence cannot occur (cross-check downgrades it).
- No independent claim can be `REPRODUCED` in 2C; repro templates stay
  rule-scoped, and `run_repro` ignores semantic subjects.
- No independent claim is emitted as folded `SUSPICIOUS`: a semantic claim
  either has evidence and is confirmed, or is retracted. This is a deliberate
  invariant on top of the general folding rule.
- The emitted `Finding` has `source=LLM_INDEPENDENT`, `rule_id=None`,
  `llm_finding_id=None`, `claim_kind` set, and the claim/trigger path from the
  stage.
- `GradingTrace` counts independent candidates, emitted findings, and
  retractions separately.

## 9. Fake, replay, and budgets

- `DeterministicFakeLLM.default_responder` returns `{"claims": []}` for
  `LLM_INDEPENDENT` unless a response is registered for the request id. The
  fake baseline therefore emits no new findings.
- A fixture may register deterministic independent responses in
  `independent_proposals.json`; `proposals.json` keeps its generation-only
  contract.
- Replay uses the 2B transcript mechanism unchanged: purpose, template version,
  prompt digest, response digest, and model identity are validated; a missing
  recording is `MISSING_RESPONSE`, with no fallback to the fake.
- The 2B budget wrapper applies. Independent requests share the per-stage
  budget with the stage that owns the client.
- Traces record calls, retries, tokens, and `replayed` exactly like 2B.

## 10. Evaluation baseline

- `EvaluationLabels`/`Label` gain optional `source: "rule" | "llm_independent"`
  (default `"rule"`) and `claim_kind: str | None`; `rule_id` becomes optional
  for independent labels only. Rule labels are unchanged.
- Independent labels name an exact source marker, resolved to a line exactly
  like rule labels, plus the expected `claim_kind`.
- Metrics report rule and independent precision/recall **separately**. The
  aggregate precision remains rule-only unless independent scanning is
  enabled; independent results are never hidden inside a combined number.
- `python -m evaluation` stays rule-only by default. `--independent` enables
  scanning and requires independent labels/proposals in the fixture; a fixture
  without them is a `FixtureError`. Both packaged fixtures ship one
  `llm_independent` defect label and an `independent_proposals.json`.
- Rule-only metrics ignore `llm_independent` labels, so the default baseline
  and calibrated threshold are unchanged. `--independent` reports 6 findings
  per packaged fixture (5 rule + 1 independent).
- `summary.md` adds an independent section only when independent scanning ran.

## 11. CLI

- `testlens audit PROJECT --independent` and
  `testlens eval --independent`; default off. `tests` is unaffected.
- All 2B model options apply (`--llm`, `--transcript`, `--record-to`,
  `--model-id`, `--max-calls`, `--max-tokens`).
- Model/transport/budget failures exit with `EXIT_MODEL=4`; no fallback.
- Reports expose rejected claims by reason, so a zero-claim run is explainable.

## 12. Test plan

Offline and fake/replay only; no provider request in CI:

1. **Anchoring** — unique quote resolves to the exact span; zero matches,
   multiple matches, overlength, and empty quotes raise the enumerated reasons.
2. **Scope** — class methods and nested functions are not requested;
   deterministic request ids and ordering; zero-claim responses are clean.
3. **Claim kinds** — each accepted kind maps to a deterministic title and
   `finding_id`; unknown kinds are rejected.
4. **Dedup** — overlap with a rule candidate rejects `DUPLICATE_OF_RULE`;
   duplicate anchors reject `DUPLICATE_CLAIM` deterministically.
5. **Cross-check** — semantic default is `unverifiable`; registered pass with
   anchored evidence yields `STATIC_CONFIRMED`; refute retracts; bad line or
   hash downgrades.
6. **Grading** — exactly one result per accepted claim; no `REPRODUCED`; no
   folded `SUSPICIOUS`; source/claim kind/ids are correct.
7. **Fake and replay** — default fake returns zero claims; a registered
   response drives one finding; the vendored transcript can be extended with
   independent recordings and replays offline.
8. **CLI/evaluation** — `--independent` off by default; on, it runs the stage
   and separates independent metrics; model failures exit `4`.
9. **Contract** — all new fields are additive; existing rule and test reports
   are unchanged when independent scanning is off.
10. **Regression** — the full v1/2A/2B suite and the rule-only evaluation
    baseline remain byte-identical (apart from timestamps).

## 13. Confirmed decisions (2026-11-17)

1. **Scope** — module-level functions only; one request per function; no cap
   beyond the model budget.
2. **Anchoring** — verbatim `quote` resolved deterministically inside the
   requested function; the model never supplies line numbers.
3. **Claim kinds** — the closed five-kind set in section 5; no runtime
   extension.
4. **Dedup** — span overlap with a rule candidate rejects the independent
   claim; duplicate anchors keep the first deterministic claim.
5. **Evidence ceiling** — independent claims cap at `STATIC_CONFIRMED`; no
   repro template in 2C.
6. **Default off** — `--audit-independent` / `--independent`; fake returns zero
   claims by default; the rule-only baseline is unchanged.
7. **Reviewer default** — semantic claims require a registered/reviewer verdict;
   the deterministic reviewer no longer auto-passes them.
8. **Report contract** — `Finding.source=LLM_INDEPENDENT`, `rule_id=None`,
   `llm_finding_id=None`, additive `claim_kind`, stable `finding_id`.
9. **Evaluation** — independent labels carry `source` + `claim_kind`; metrics
   are reported separately; the baseline stays rule-only by default.
10. **Failure behavior** — model/transport/budget failures are fatal
    (`EXIT_MODEL=4`); rejections are enumerated and reported, never silently
    dropped.

## 14. Acceptance criteria

- No behavior change when independent scanning is off; the existing suite,
  evaluation baseline, and report contracts still pass.
- Every accepted claim has a deterministic file/span/id and structured
  cross-check evidence; every rejected claim has an enumerated reason.
- No independent finding exceeds `STATIC_CONFIRMED`, and none is folded as
  `SUSPICIOUS`.
- Fake and replay paths work offline; CI never contacts a provider.
- `CLAUDE.md` is updated with the confirmed decisions before implementation
  begins.
