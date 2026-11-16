from __future__ import annotations

import ast
import json

from llm.fake import DeterministicFakeLLM, FAKE_MODEL_ID, default_responder
from llm.models import (
    FinishReason,
    LLMPurpose,
    LLMRequest,
    ModelKind,
)

TARGET_BLOCK = json.dumps(
    {
        "symbol_id": "app.py::read_item",
        "module": "app",
        "name": "read_item",
        "file": "app.py",
        "line_start": 3,
        "line_end": 4,
        "is_async": False,
        "parameters": [{"name": "item_id", "has_default": False, "kind": "positional_or_keyword"}],
        "routes": [{"methods": ["GET"], "path": "/items/{item_id}", "decorator": "app.get"}],
        "source": "def read_item(item_id):\n    return {'item_id': item_id}",
    },
    sort_keys=True,
    indent=2,
)


def _request(
    prompt: str = "",
    *,
    purpose: LLMPurpose = LLMPurpose.TEST_GENERATION,
    request_id: str = "test_generation::app.py::read_item",
) -> LLMRequest:
    return LLMRequest(
        request_id=request_id,
        purpose=purpose,
        prompt_template_version="generation-v1",
        system="system",
        prompt=prompt or f"<TARGET_JSON>\n{TARGET_BLOCK}\n</TARGET_JSON>\n",
    )


def test_fake_is_deterministic_and_labels_itself():
    request = _request()
    fake = DeterministicFakeLLM()

    first = fake.complete(request)
    second = fake.complete(request)

    assert first.text == second.text
    assert first.response_digest == second.response_digest
    assert first.model_kind == ModelKind.FAKE
    assert first.model_id == FAKE_MODEL_ID
    assert first.finish_reason == FinishReason.STOP
    assert fake.calls[0].prompt_digest == fake.calls[1].prompt_digest


def test_fake_prefers_registered_response_for_request_id():
    fake = DeterministicFakeLLM({"test_generation::app.py::read_item": '{"tests": []}'})

    response = fake.complete(_request())

    assert response.text == '{"tests": []}'
    assert response.response_digest


def test_request_digest_changes_with_prompt():
    first = _request("first prompt").prompt_digest
    second = _request("second prompt").prompt_digest

    assert first != second


def test_default_responder_emits_a_parseable_smoke_test():
    response = default_responder(_request())
    payload = json.loads(response)

    assert list(payload) == ["tests"]
    proposal = payload["tests"][0]
    assert proposal["test_file"] == "generated/test_app_read_item.py"
    assert proposal["test_name"] == "test_read_item_smoke"
    # item_id has no default, so the fake only asserts the handler is callable.
    assert "assert callable(read_item)" in proposal["source"]
    ast.parse(proposal["source"])


def test_default_responder_calls_zero_argument_handlers():
    target = json.loads(TARGET_BLOCK)
    target["name"] = "status"
    target["parameters"] = []
    prompt = f"<TARGET_JSON>\n{json.dumps(target, sort_keys=True)}\n</TARGET_JSON>\n"

    payload = json.loads(default_responder(_request(prompt)))

    assert payload["tests"][0]["test_name"] == "test_status_smoke"
    assert "    status()" in payload["tests"][0]["source"]


def test_unsupported_purpose_returns_empty_completion():
    response = DeterministicFakeLLM().complete(
        _request(purpose=LLMPurpose.RULE_TRIAGE)
    )

    assert response.text == ""
    assert response.finish_reason == FinishReason.EMPTY
