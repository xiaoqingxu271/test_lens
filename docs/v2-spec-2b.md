# TestLens v2 specification — 2B real LLM client and replay

**Status: ACCEPTED 2026-11-16. `CLAUDE.md` carries the normative summary; this
document holds the full design.**

This document specifies only increment 2B: a real model client behind the
existing `LLMClient` interface, plus deterministic record/replay. It does not
change the pipelines, rules, mutation operators, or the `LLM_INDEPENDENT`
path.

## 1. Goal

Allow a real model to drive test generation and rule triage while preserving
the v1 guarantees:

- the deterministic fake remains the default;
- CI never contacts a model provider;
- every real-model run is labeled `ModelKind.REAL`;
- a recorded run can be replayed byte-for-byte without network access;
- model failures are explicit, enumerated, and never silently replaced by the
  fake;
- the expected output contract (`LLMRequest` / `LLMResponse` / `LLMClient`)
  stays intact, with optional additive fields only.

## 2. Non-goals for 2B

- `LLM_INDEPENDENT` findings. Triage still works only inside rule candidates.
- Semantic line localization by the model; deterministic AST location remains
  authoritative.
- Multi-provider routing, automatic provider fallback, or model comparison
  dashboards.
- Fine-tuning, embeddings, vector stores, agents, or tool-use loops.
- Streaming responses. The interface stays request/response.
- Running a real-model evaluation in normal CI.
- Storing prompts in transcripts.

## 3. Current contract and invariants

`llm/models.py` already defines:

- `LLMRequest`: deterministic `request_id`, purpose, template version,
  system/user prompt, seed, temperature, computed `prompt_digest`.
- `LLMResponse`: text, `model_id`, `model_kind`, finish reason, usage, computed
  `response_digest`.
- `LLMClient`: `model_kind`, `model_id`, `complete(request)`.

2B adds only optional fields:

```python
class LLMResponse(BaseModel):
    # existing fields unchanged
    attempts: int = 1
    replayed: bool = False


class FinishReason(str, Enum):
    # existing values unchanged
    UNKNOWN = "unknown"
```

`LLMRequest` does not change. New clients are classes in `llm/`; no other
package imports a provider SDK or reads provider credentials.

## 4. Components

```
llm/
  models.py        # existing contracts + optional fields
  fake.py          # existing deterministic fake (default)
  replay.py        # transcript models, loader, ReplayLLM
  recording.py     # RecordingLLM wrapper, atomic transcript writer
  real.py          # provider transport protocol + first adapter
  budget.py        # optional call/token budget wrapper
  errors.py        # enumerated model, replay, and budget errors
```

Composition order for a real run:

```
BudgetedLLM(
  RecordingLLM(
    RealLLM(transport, config),
    transcript_path,
  ),
  budget,
)
```

For replay:

```
BudgetedLLM(ReplayLLM(transcript_path), budget)
```

The pipeline stages still receive only `LLMClient`; they cannot tell whether
the client is fake, real, or replay except through `model_kind`, `model_id`,
and `LLMResponse.replayed`.

## 5. Transcript format

A transcript is one versioned JSON object, written atomically (temporary file
then replace):

```json
{
  "transcript_version": 1,
  "created_by": "testlens 2.0.0",
  "model_id": "<provider model id>",
  "model_kind": "real",
  "recordings": [
    {
      "request_id": "test_generation::app.py::classify",
      "purpose": "test_generation",
      "prompt_template_version": "generation-v1",
      "prompt_digest": "0123456789abcdef",
      "response_digest": "fedcba9876543210",
      "text": "<response text>",
      "model_id": "<provider model id>",
      "model_kind": "real",
      "finish_reason": "stop",
      "attempts": 1,
      "usage": {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150
      }
    }
  ]
}
```

Rules:

- Recordings are sorted by `request_id` before writing, so diffs are stable.
- `request_id` is unique. A duplicate is a `TranscriptError`.
- The prompt itself is not stored; `prompt_digest` and template version are.
- The response text is stored verbatim because replay must produce the same
  output.
- `response_digest` is recomputed on load; a tampered text fails.
- Transcript files contain model output and may contain project code. Do not
  commit real transcripts. Vendored replay fixtures for tests live under
  `tests/fixtures/llm/`.

## 6. Replay semantics

`ReplayLLM` is a deterministic `LLMClient`:

- Loads and validates the transcript at construction time.
- Looks up a response by `request_id`; it never calls a responder or the
  network.
- Verifies `purpose`, `prompt_template_version`, and `prompt_digest` against
  the request.
- Recomputes `response_digest` and compares it with the recording.
- Returns `model_kind=REAL` and `replayed=True`, preserving the recorded
  `model_id`, text, finish reason, attempts, and usage.
- Does not read the clock, randomness, or environment.

Enumerated replay failures:

- `TRANSCRIPT_UNREADABLE`
- `INVALID_TRANSCRIPT`
- `UNSUPPORTED_TRANSCRIPT_VERSION`
- `DUPLICATE_REQUEST`
- `MISSING_RESPONSE`
- `PURPOSE_MISMATCH`
- `PROMPT_DIGEST_MISMATCH`
- `TEMPLATE_VERSION_MISMATCH`
- `RESPONSE_DIGEST_MISMATCH`
- `MODEL_MISMATCH` (when an expected model id is configured)

No failure falls back to the fake or to another recording.

## 7. Recording semantics

`RecordingLLM` wraps another client and writes a transcript:

- Calls the inner client, then records the request metadata and the full
  response.
- Writes atomically after every successful completion so a crash does not
  leave a truncated JSON file.
- Raises `RecordingError` if the transcript cannot be written; it never
  silently drops a recording.
- Refuses to start if the destination already exists, unless an explicit
  `overwrite=True` is passed.
- Asserts that the inner response's `request_id` matches the request.
- Never records the system prompt, user prompt, API key, or HTTP headers.

## 8. Real client semantics

`RealLLM` is the first provider adapter behind a small transport protocol:

```python
class ProviderTransport(Protocol):
    def complete(
        self, request: LLMRequest, config: RealLLMConfig
    ) -> ProviderResult: ...
```

Configuration:

```python
class RealLLMConfig(BaseModel):
    model_id: str
    api_key_env: str = "TESTLENS_LLM_API_KEY"
    base_url: str | None = None
    timeout_seconds: float = 60.0
    max_attempts: int = 3
    max_output_tokens: int | None = None
    temperature: float = 0.0
```

Rules:

- Credentials are read from the named environment variable at call time and
  never stored, logged, or written into reports or transcripts.
- HTTPS only; certificate verification stays enabled. No insecure switch.
- `model_kind=REAL`.
- Map provider finish reasons to `STOP`, `LENGTH`, `REFUSAL`, `EMPTY`, or
  `UNKNOWN`.
- A response with empty text returns `FinishReason.EMPTY`; stage-level
  validation rejects it with the existing enumerated reasons.
- The adapter validates response shape and rejects unknown fields it depends
  on, without guessing.

Retry policy:

- Retry only on timeouts, transport errors, HTTP 429, and HTTP 5xx.
- At most `max_attempts` attempts; exponential backoff 1s, 2s, 4s with no
  jitter.
- Never retry 401/403/404 or an invalid successful response.
- `attempts` reports the actual number of attempts.
- Retries never change `request_id`.

Enumerated errors:

- `LLM_AUTH_ERROR`
- `LLM_TIMEOUT`
- `LLM_RATE_LIMIT`
- `LLM_TRANSPORT_ERROR`
- `LLM_RESPONSE_ERROR`
- `LLM_BUDGET_EXCEEDED`

Model errors are fatal for the current stage. They are never converted into
empty proposals, skipped candidates, or fake responses.

## 9. Budget

`BudgetedLLM` enforces per-run limits:

```python
class LLMBudget(BaseModel):
    max_calls: int = 200
    max_total_tokens: int = 500_000
```

- `max_calls` is checked before the request; `max_total_tokens` after a
  response whose usage is known.
- Exceeding either raises `LLM_BUDGET_EXCEEDED`.
- Budget is per stage invocation, not persisted.
- `None` disables a limit, but the CLI always supplies defaults.

## 10. Stage tracing and labeling

Optional additive trace fields:

- `GenerationTrace`: `llm_calls`, `llm_retries`, `prompt_tokens`,
  `completion_tokens`, `replayed`.
- `TriageTrace`: the same fields.
- `EvaluationTrace`: the same fields aggregated across fixtures.

Reports keep `model_kind` and `model_id`. A replayed real run reports
`model_kind=real` and `replayed=true`; it is never mislabeled as a fake run.
A real or replay run is excluded from deterministic baseline comparisons
unless the report is read explicitly.

## 11. CLI and evaluation surface

New options on `testlens tests`, `testlens audit`, and `testlens eval`:

```
--llm {fake,replay,real}          # default: fake
--transcript PATH                 # required for replay
--record-to PATH                  # optional for real
--model-id ID                     # required for real
--max-calls N                     # default: 200
--max-tokens N                    # default: 500000
```

Rules:

- `--transcript` requires `--llm replay`; `--record-to` requires `--llm real`.
  Any other combination is a usage error.
- `--llm fake` rejects transcript and recording options.
- `--llm replay` requires `--transcript`.
- `--llm real` requires `--model-id` and a configured API-key environment
  variable; `--record-to` is optional.
- Model/auth/transport/budget failures exit with a new `EXIT_MODEL = 4`;
  shape/usage errors remain `EXIT_USAGE = 2`.
- CI runs only `--llm fake` and replay tests; no workflow step ever passes
  `--llm real`.
- `python -m evaluation` accepts the same options. A real or replay evaluation
  writes under a model-labeled subdirectory, never overwriting the fake
  baseline.

## 12. Test plan

Offline tests only, no external network:

1. **Transcript round trip** — recording then replay yields identical
   responses, usage, and digests.
2. **Determinism** — transcript ordering does not change replay output;
   repeated replay is byte-identical.
3. **Tampering** — changed text, prompt digest, template version, duplicate
   request ids, missing responses, and unsupported versions raise the
   enumerated errors.
4. **No fallback** — replay misses raise; the fake is never consulted.
5. **Recorder safety** — atomic write, refusal to overwrite, write failures
   surface as `RecordingError`, prompts and API keys are absent from the file.
6. **Real adapter** — injected fake transport tests retry classification,
   auth/rate-limit/timeout mapping, finish-reason mapping, and redaction.
   A socket guard test proves the default suite makes no outbound connection.
7. **Budget** — call and token limits raise before overrun; disabled limits
   pass.
8. **CLI** — option validation, exit code 4, and transcript requirements.
9. **Contract** — `LLMRequest`, `LLMResponse`, and `LLMClient` keep all
   existing fields; new fields are optional and defaulted.
10. **Replay fixture** — a small vendored transcript exercises generation and
    triage end-to-end without a provider.

Real-provider smoke tests are manual, documented, and never part of CI.

## 13. Confirmed decisions (2026-11-16)

1. **Provider scope** — one provider adapter first; keep the transport protocol
   provider-neutral. The provider/model is chosen by the operator. Do not build
   multi-provider routing in 2B.
2. **Transcript contents** — store response text and digests, never prompts or
   headers.
3. **Replay labeling** — report `model_kind=real` plus `replayed=true`; never
   label a replay as fake.
4. **Budget defaults** — `max_calls=200`, `max_total_tokens=500_000`,
   configurable per run.
5. **Retry policy** — 3 attempts, 1s/2s/4s backoff, only timeouts/429/5xx.
6. **CLI surface** — `--llm`, `--transcript`, `--record-to`, `--model-id`,
   `--max-calls`, `--max-tokens` on all three commands.
7. **Real baseline** — manual only, separate output; CI runs fake and offline
   replay tests.
8. **Exit code** — add `EXIT_MODEL = 4` for model/transport/budget failures.

## 14. Acceptance criteria for 2B

- The default suite and CI make zero external network requests.
- The deterministic fake remains the default for all three commands.
- A recorded transcript replays exactly, with tamper and mismatch failures
  enumerated.
- Real-model reports are labeled `real`; replay reports are labeled `real` with
  `replayed=true`.
- Model failures never fall back to the fake or to empty successful output.
- Prompts, API keys, and headers never appear in transcripts, reports, or
  logs.
- `CLAUDE.md` is updated with the confirmed decisions before implementation
  begins.
