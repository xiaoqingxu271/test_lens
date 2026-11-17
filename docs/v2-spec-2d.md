# TestLens v2 specification — 2D LLM claim reviewer

**Status: ACCEPTED 2026-11-17.** This document records the accepted design for
increment 2D; `CLAUDE.md` carries the normative summary.

This document specifies increment 2D: an LLM-backed reviewer for semantic
(`rule_id=None`) claims. It closes the gap left by 2C, where the CLI always
used the deterministic reviewer and therefore retracted every independent
claim as `UNVERIFIABLE_CLAIM`. It does not change the test pipeline, the rule
set, the claim-kind set, or the 2B provider layer.

## 1. Goal

Make `LLM_INDEPENDENT` findings producible outside tests and fixture evaluation
while preserving every existing guarantee:

- the model still only proposes; the stage owns file selection, evidence
  anchoring, verdict validation, and evidence levels;
- semantic claims require structured evidence, re-anchored deterministically;
- rule-backed claims never reach the LLM reviewer: deterministic rules stay
  authoritative;
- the deterministic fake remains the default, and CI makes no provider call;
- model, transport, and budget failures stay fatal and never become an
  unverifiable verdict or an empty finding.

## 2. Non-goals for 2D

- Cross-file evidence. In 2D every evidence line must live in the subject
  file, so the file path is never model-supplied.
- Model-chosen line numbers, `finding_id`s, or retract reasons.
- Reviewing rule-backed findings, or overriding a deterministic rule verdict.
- Semantic reproduction or a `REPRODUCED` ceiling for independent claims.
- New claim kinds, new rules, or changes to `audit/independent.py`.
- An LLM reviewer inside `eval`; 2D adds it to `audit` only.

## 3. Contract additions

All additions are optional or new records; no existing field is renamed or
removed.

```python
class LLMPurpose(str, Enum):
    TEST_GENERATION = "test_generation"
    RULE_TRIAGE = "rule_triage"
    LLM_INDEPENDENT = "llm_independent"
    CLAIM_REVIEW = "claim_review"        # new


class ReviewRejectReason(str, Enum):     # new, review/models.py
    EMPTY_RESPONSE = "empty_response"
    INVALID_JSON = "invalid_json"
    INVALID_SHAPE = "invalid_shape"
    UNKNOWN_VERDICT = "unknown_verdict"
    UNKNOWN_ROLE = "unknown_role"
    UNKNOWN_KIND = "unknown_kind"
    EMPTY_EXPLANATION = "empty_explanation"
    EMPTY_QUOTE = "empty_quote"
    QUOTE_TOO_LONG = "quote_too_long"
    UNLOCATABLE_QUOTE = "unlocatable_quote"
    AMBIGUOUS_QUOTE = "ambiguous_quote"
```

Additive fields:

- `ReviewerVerdict.rejection_reason: ReviewRejectReason | None = None`
- `CrossCheckResult.rejection_reason: ReviewRejectReason | None = None`
- `CrossCheckTrace.review_rejections: dict[ReviewRejectReason, int] = {}`

`ReviewerKind.MODEL` already exists and is used by the new reviewer. No
`CrossCheckResult` field changes otherwise.

New reviewer in `review/llm_reviewer.py`:

```python
class LLMReviewer:
    reviewer_kind = ReviewerKind.MODEL
    reviewer_id = "llm-reviewer-v1"

    def __init__(self, profile: ProjectProfile, llm: LLMClient) -> None: ...
    def review(self, request: ReviewRequest) -> ReviewerVerdict: ...
```

`LLMReviewer` may import `llm.models` and `llm.base`; it must not import
`audit.triage`, `audit.independent`, `llm.fake`, or `review.fake` internals.

## 4. Reviewer selection and flow

Cross-check keeps its current routing:

```
subject.rule_id is not None  -> RuleReviewer (authoritative)
subject.rule_id is None      -> the configured ClaimReviewer
```

The configured reviewer is chosen by the CLI:

- `--reviewer deterministic` (default) — current behavior; semantic claims
  without a registered verdict return `unverifiable`/`UNVERIFIABLE_CLAIM`.
- `--reviewer llm` — constructs `LLMReviewer(profile, llm)`.

Constraints:

- `audit --reviewer llm` requires `--independent`; otherwise it is a usage
  error (`EXIT_USAGE=2`).
- `--reviewer llm` works with `--llm fake`, `replay`, or `real`. The default
  fake returns `unverifiable` for claim-review requests, so `--proposals` is
  needed to seed verdicts in fake mode.
- `eval` keeps the fixture-registered deterministic reviewer in 2D; no
  `--reviewer` option is added there.
- `--reviewer` is not added to `tests`.

## 5. Request template

Template version `review-v1`. One request per semantic subject, with the
existing deterministic id `cross_check::<finding_id>`:

```python
LLMRequest(
    request_id=f"cross_check::{request.finding_id}",
    purpose=LLMPurpose.CLAIM_REVIEW,
    prompt_template_version="review-v1",
    system=REVIEW_SYSTEM_PROMPT,
    prompt=build_review_prompt(request),   # <REVIEW_JSON> block
    seed=0,
    temperature=0.0,
)
```

`build_review_prompt` serializes the existing `ReviewRequest` fields
deterministically: finding id, file, subject span, claim, trigger path,
subject snippet and hash, and the assembled `ReviewContext` (imports,
enclosing function, routes, callers, type hints, guards, other rule hits). It
never contains triage reasoning, prior reviewer output, or an expected
verdict.

## 6. Response shape and deterministic evidence anchoring

The model returns one JSON object:

```json
{
  "verdict": "pass",
  "evidence": [
    {
      "quote": "    limit = int(request.args['limit'])",
      "kind": "data_flow",
      "role": "supports",
      "explanation": "the raw query value reaches int() with no validation"
    }
  ]
}
```

Rules:

- `verdict` must be `pass`, `refute`, or `unverifiable`.
- `evidence` must be a list (possibly empty for `unverifiable`).
- Each item requires a non-empty `quote` (≤ 400 characters, CRLF normalized),
  an `EvidenceKind` value, an `EvidenceRole` value, and a non-empty
  `explanation`. Unknown values are rejected with an enumerated reason.
- The model never supplies `file`, `line_start`, `line_end`, `snippet`,
  `snippet_hash`, `retract_reason`, or `checked_rule_ids`.
- The stage anchors each quote inside the subject file:
  - exactly one occurrence → resolve `line_start`/`line_end`, compute
    `snippet_hash` with the shared review hash, and set `file` to the subject
    file;
  - zero → `UNLOCATABLE_QUOTE`; more than one → `AMBIGUOUS_QUOTE`;
  - empty → `EMPTY_QUOTE`; overlength → `QUOTE_TOO_LONG`.
- Any rejection makes the whole verdict `unverifiable` with
  `UNVERIFIABLE_CLAIM` and the rejection reason recorded in
  `ReviewerVerdict.rejection_reason`. Evidence is never partially accepted.
- `pass` with zero `supports` lines, or `refute` with zero `refutes` lines, is
  downgraded by the existing cross-check validation to `unverifiable`; the
  reviewer does not paper over it.
- Retract reasons are stage-assigned exactly as today: `refute` defaults to
  `REFUTED_BY_CONTEXT`, `unverifiable` to `UNVERIFIABLE_CLAIM`.
- The existing `_validated_verdict` pass still re-checks every required line's
  location and `snippet_hash`; the LLM reviewer's anchors must survive it.

## 7. Cross-check trace and attribution

- `model_reviews` increments once per semantic subject, regardless of the
  outcome.
- `review_rejections` aggregates `ReviewRejectReason` values from all model
  verdicts.
- `CrossCheckResult.rejection_reason` carries the per-subject reason.
- `reviewer_ids` includes `llm-reviewer-v1` alongside any rule reviewer.
- A model identity mismatch between `LLMClient` and `LLMResponse` adds a
  warning; it does not silently relabel the reviewer.

## 8. Fake, replay, and budgets

- `DeterministicFakeLLM.default_responder` returns
  `{"verdict": "unverifiable", "evidence": []}` for `CLAIM_REVIEW` unless a
  response is registered for the request id. The fake baseline therefore
  produces no new findings.
- `--proposals FILE` may seed `cross_check::<finding_id>` responses in fake
  mode.
- Replay uses the 2B transcript mechanism unchanged: purpose, template
  version, prompt digest, response digest, and model identity are validated.
  A missing recording is `MISSING_RESPONSE`; there is no fallback to the fake.
  Transcripts still store no prompts, system messages, API keys, or headers.
- The 2B budget wrapper applies. Review calls share the per-run budget with
  the audit stage that owns the client; exhaustion raises
  `LLM_BUDGET_EXCEEDED` and exits `EXIT_MODEL=4`.
- Retries and real-model credentials follow 2B exactly.

## 9. Grading and evidence ceiling

No grading change:

- `pass` with valid supports evidence → `STATIC_CONFIRMED`.
- `refute` → `Retracted` with `REFUTED_BY_CONTEXT`.
- `unverifiable` → `Retracted` with `UNVERIFIABLE_CLAIM`.
- Independent claims still never reach `REPRODUCED` and are never folded as
  `SUSPICIOUS`.
- Rule-backed findings are unaffected in every mode.

## 10. CLI

- `testlens audit PROJECT --independent --reviewer llm [--llm ...]`.
- `--reviewer` accepts `deterministic` (default) and `llm`.
- `--reviewer llm` without `--independent` exits `2` with a usage message.
- All 2B model options apply (`--llm`, `--transcript`, `--record-to`,
  `--model-id`, `--max-calls`, `--max-tokens`).
- Model/transport/budget failures exit `EXIT_MODEL=4`; no fallback.
- Report rejection counts are visible in the cross-check trace and the JSON
  report, so a zero-finding independent run is explainable.

## 11. Test plan

Offline and fake/replay only; no provider request in CI:

1. **Parsing** — valid `pass`/`refute`/`unverifiable`; malformed JSON, wrong
   shape, unknown verdict/role/kind, empty explanation, empty/overlong quote
   map to enumerated `ReviewRejectReason` values and `unverifiable`.
2. **Anchoring** — unique quote resolves file/span/hash inside the subject
   file; zero matches and multiple matches reject; model-supplied line fields
   are absent from the schema and ignored if present in raw JSON.
3. **Cross-check integration** — semantic `pass` with anchored supports
   becomes `STATIC_CONFIRMED`; `refute` retracts with `REFUTED_BY_CONTEXT`;
   rejected evidence retracts with `UNVERIFIABLE_CLAIM`; rule-backed subjects
   never invoke the LLM reviewer.
4. **Independence** — `LLMReviewer` has its own module, state, and reviewer id;
   it does not import or share state with the triage fake or the
   deterministic reviewer.
5. **Fake and replay** — default fake returns `unverifiable`; seeded fake
   proposals drive one confirmed finding; record/replay of a `claim_review`
   transcript reproduces the same verdict offline; a missing recording raises
   `MISSING_RESPONSE`.
6. **CLI** — `--reviewer llm` requires `--independent`; a seeded fake run
   emits one `LLM_INDEPENDENT` finding; replay without a recording exits `4`;
   the default deterministic path is unchanged.
7. **Contract** — new fields are additive; existing rule, 2C, and test reports
   are unchanged when the LLM reviewer is off.
8. **Regression** — the full suite and both evaluation baselines
   (`python -m evaluation`, `--independent`) are unchanged.

## 12. Confirmed decisions (2026-11-17)

1. **Reviewer selection** — explicit `--reviewer {deterministic,llm}`,
   default deterministic; `llm` requires `--independent`; `eval` unchanged.
2. **Scope** — semantic claims only; rule-backed claims stay with
   `RuleReviewer`.
3. **Prompt contract** — one `review-v1` request per subject using the
   existing `ReviewRequest` and deterministic context; no triage reasoning.
4. **Response contract** — `verdict` plus `evidence` items of quote/kind/role/
   explanation; no model-supplied file, lines, hashes, or retract reasons.
5. **Anchoring** — verbatim quote, unique within the subject file; ambiguous
   or unlocatable evidence rejects the whole verdict.
6. **Rejection attribution** — `ReviewRejectReason` on the verdict/result and
   aggregated in `CrossCheckTrace.review_rejections`.
7. **Defaults** — fake returns `unverifiable`; deterministic default behavior
   is unchanged; no silent fallback.
8. **Evidence ceiling** — unchanged; independent claims cap at
   `STATIC_CONFIRMED` and never fold as `SUSPICIOUS`.
9. **Failure behavior** — model/transport/budget failures fatal (`EXIT_MODEL=4`);
   rejected claims enumerated, never silently dropped.
10. **Evaluation** — no LLM reviewer in `eval` in 2D; fixture-registered
    verdicts remain authoritative for the offline baseline.

## 13. Acceptance criteria

- `audit --independent --reviewer llm --llm replay --transcript FILE` runs
  fully offline and yields either a `STATIC_CONFIRMED` independent finding
  with re-anchored evidence or a retraction with an enumerated reason.
- No rule-backed verdict changes in any mode.
- No independent finding exceeds `STATIC_CONFIRMED`; none is folded as
  `SUSPICIOUS`.
- Fake, replay, and real paths share one contract; CI never contacts a
  provider.
- `CLAUDE.md` is updated with the confirmed decisions before implementation
  begins.
