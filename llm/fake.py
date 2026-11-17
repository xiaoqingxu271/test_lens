"""Deterministic fake model used by default and by the evaluation harness.

The fake never touches the network, the clock, or a random source. Responses
are resolved in two steps:

1. an explicitly registered response for the request id (tests and fixtures);
2. a deterministic responder derived from the structured context the stage
   places inside the prompt.

The default responder is a stand-in, not a quality bar: its proposals are
deliberately simple and are expected to be filtered later by the sandbox,
mutation scoring, and attribution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from .models import (
    FinishReason,
    LLMCallStats,
    LLMRequest,
    LLMResponse,
    LLMPurpose,
    ModelKind,
)

FAKE_MODEL_ID = "fake:deterministic-v1"

_TARGET_BLOCK = re.compile(r"<TARGET_JSON>\n(.*?)\n</TARGET_JSON>", re.DOTALL)
_CANDIDATE_BLOCK = re.compile(
    r"<CANDIDATE_JSON>\n(.*?)\n</CANDIDATE_JSON>", re.DOTALL
)

Responder = Callable[[LLMRequest], str]


def _extract_block(prompt: str, pattern: re.Pattern[str]) -> dict[str, Any] | None:
    match = pattern.search(prompt)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def extract_target_context(prompt: str) -> dict[str, Any] | None:
    return _extract_block(prompt, _TARGET_BLOCK)


def extract_candidate_context(prompt: str) -> dict[str, Any] | None:
    return _extract_block(prompt, _CANDIDATE_BLOCK)


def _has_required_parameter(parameters: list[dict[str, Any]]) -> bool:
    for parameter in parameters:
        if parameter.get("has_default"):
            continue
        if parameter.get("kind") in {
            "positional_only",
            "positional_or_keyword",
            "keyword_only",
        }:
            return True
    return False


def _smoke_test_response(target: dict[str, Any]) -> str:
    name = target.get("name")
    module = target.get("module")
    relative_file = target.get("file")
    if not isinstance(name, str) or not isinstance(module, str) or not isinstance(
        relative_file, str
    ):
        return json.dumps({"tests": []}, sort_keys=True)

    stem = relative_file.removesuffix(".py").replace("/", "_")
    test_file = f"generated/test_{stem}_{name}.py"
    test_name = f"test_{name}_smoke"
    parameters = target.get("parameters")
    parameters = parameters if isinstance(parameters, list) else []

    if _has_required_parameter(parameters):
        body = f"    assert callable({name})"
    else:
        body = f"    {name}()"
    source = f"from {module} import {name}\n\n\ndef {test_name}():\n{body}\n"
    return json.dumps(
        {
            "tests": [
                {
                    "test_file": test_file,
                    "test_name": test_name,
                    "source": source,
                }
            ]
        },
        sort_keys=True,
    )


def _triage_response(candidate: dict[str, Any]) -> str:
    finding_id = candidate.get("finding_id")
    rule_id = candidate.get("rule_id")
    line_start = candidate.get("line_start")
    line_end = candidate.get("line_end")
    if not isinstance(finding_id, str) or not isinstance(rule_id, str):
        return ""
    if not isinstance(line_start, int) or not isinstance(line_end, int):
        return ""

    snippet = candidate.get("snippet")
    title = candidate.get("title")
    anchor = f"{candidate.get('file')}:{line_start}-{line_end}"
    trigger_path = [anchor]
    if isinstance(snippet, str) and snippet.strip():
        trigger_path.append(snippet.strip())
    elif isinstance(title, str) and title.strip():
        trigger_path.append(title.strip())

    claim = candidate.get("message")
    if not isinstance(claim, str) or not claim.strip():
        claim = title if isinstance(title, str) else ""
    return json.dumps(
        {
            "finding_id": finding_id,
            "rule_id": rule_id,
            "line_start": line_start,
            "line_end": line_end,
            "trigger_path": trigger_path,
            "claim": claim,
        },
        sort_keys=True,
    )


def default_responder(request: LLMRequest) -> str:
    """Deterministic stand-in response for a request."""
    if request.purpose is LLMPurpose.TEST_GENERATION:
        target = extract_target_context(request.prompt)
        if target is None:
            return json.dumps({"tests": []}, sort_keys=True)
        return _smoke_test_response(target)
    if request.purpose is LLMPurpose.RULE_TRIAGE:
        candidate = extract_candidate_context(request.prompt)
        if candidate is None:
            return ""
        return _triage_response(candidate)
    if request.purpose is LLMPurpose.LLM_INDEPENDENT:
        # The default fake proposes no semantic claims: independent findings
        # must be explicitly seeded so they cannot inflate precision.
        return json.dumps({"claims": []}, sort_keys=True)
    if request.purpose is LLMPurpose.CLAIM_REVIEW:
        # The default fake never confirms a semantic claim; verdicts must be
        # explicitly seeded or recorded.
        return json.dumps(
            {"verdict": "unverifiable", "evidence": []}, sort_keys=True
        )
    return ""


class DeterministicFakeLLM:
    """Fake ``LLMClient`` keyed by deterministic request ids."""

    model_kind = ModelKind.FAKE

    def __init__(
        self,
        responses: Mapping[str, str] | None = None,
        *,
        model_id: str = FAKE_MODEL_ID,
        responder: Responder | None = None,
    ) -> None:
        self.model_id = model_id
        self._responses = dict(responses or {})
        self._responder = responder or default_responder
        self.calls: list[LLMRequest] = []
        self.stats = LLMCallStats()

    def complete(self, request: LLMRequest) -> LLMResponse:
        text = self._responses.get(request.request_id)
        if text is None:
            text = self._responder(request)
        self.calls.append(request)
        self.stats.calls += 1
        return LLMResponse(
            request_id=request.request_id,
            text=text,
            model_id=self.model_id,
            model_kind=self.model_kind,
            finish_reason=FinishReason.EMPTY if not text.strip() else FinishReason.STOP,
        )
