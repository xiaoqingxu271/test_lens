from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from llm.errors import LLMError, LLMErrorReason
from llm.models import FinishReason, LLMPurpose, LLMRequest, LLMUsage
from llm.real import (
    API_KEY_ENV,
    DEFAULT_BASE_URL,
    OpenAICompatibleTransport,
    ProviderResult,
    RealLLM,
    RealLLMConfig,
    _finish_reason,
    read_api_key,
)

REQUEST = LLMRequest(
    request_id="test_generation::app.py::read_item",
    purpose=LLMPurpose.TEST_GENERATION,
    prompt_template_version="generation-v1",
    system="system prompt",
    prompt="user prompt",
)


def _config(**overrides) -> RealLLMConfig:
    base = {"model_id": "model-x", "base_url": "https://example.test/v1"}
    base.update(overrides)
    return RealLLMConfig(**base)


class _ScriptedTransport:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.seen: list[LLMRequest] = []

    def complete(self, request: LLMRequest, config: RealLLMConfig) -> ProviderResult:
        self.seen.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Sleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _llm(transport, **overrides) -> tuple[RealLLM, _Sleeper]:
    sleeper = _Sleeper()
    llm = RealLLM(_config(**overrides), transport=transport, sleep=sleeper)
    return llm, sleeper


def _result(text: str = '{"tests": []}', **overrides) -> ProviderResult:
    base = {"text": text}
    base.update(overrides)
    return ProviderResult(**base)


def test_successful_completion_maps_text_usage_and_identity():
    transport = _ScriptedTransport(
        _result(
            "hello",
            model_id="server-model",
            finish_reason=FinishReason.LENGTH,
            usage=LLMUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7),
        )
    )
    llm, sleeper = _llm(transport)

    response = llm.complete(REQUEST)

    assert response.text == "hello"
    assert response.model_id == "server-model"
    assert response.model_kind.value == "real"
    assert response.finish_reason is FinishReason.LENGTH
    assert response.usage.total_tokens == 7
    assert response.attempts == 1
    assert response.replayed is False
    assert llm.stats.calls == 1
    assert llm.stats.prompt_tokens == 3
    assert llm.stats.completion_tokens == 4
    assert sleeper.delays == []


def test_provider_model_id_falls_back_to_the_configured_id():
    llm, _ = _llm(_ScriptedTransport(_result("hello")))

    assert llm.complete(REQUEST).model_id == "model-x"


@pytest.mark.parametrize(
    "reason",
    [
        LLMErrorReason.TIMEOUT,
        LLMErrorReason.RATE_LIMIT,
        LLMErrorReason.TRANSPORT,
    ],
)
def test_retryable_failures_are_retried_with_backoff(reason):
    error = LLMError("transient", reason=reason)
    transport = _ScriptedTransport(error, error, _result("ok"))
    llm, sleeper = _llm(transport)

    response = llm.complete(REQUEST)

    assert response.attempts == 3
    assert len(transport.seen) == 3
    assert sleeper.delays == [1.0, 2.0]
    assert llm.stats.retries == 2
    assert llm.stats.calls == 1


@pytest.mark.parametrize("reason", [LLMErrorReason.AUTH, LLMErrorReason.RESPONSE])
def test_permanent_failures_are_not_retried(reason):
    transport = _ScriptedTransport(
        LLMError("permanent", reason=reason), _result("unused")
    )
    llm, sleeper = _llm(transport)

    with pytest.raises(LLMError) as excinfo:
        llm.complete(REQUEST)

    assert excinfo.value.reason is reason
    assert len(transport.seen) == 1
    assert sleeper.delays == []
    assert llm.stats.calls == 0


def test_retry_exhaustion_raises_the_last_error():
    error = LLMError("still down", reason=LLMErrorReason.TRANSPORT)
    transport = _ScriptedTransport(error, error, error)
    llm, sleeper = _llm(transport)

    with pytest.raises(LLMError) as excinfo:
        llm.complete(REQUEST)

    assert excinfo.value is error
    assert sleeper.delays == [1.0, 2.0]
    assert llm.stats.retries == 2


def test_single_attempt_configuration_does_not_retry():
    error = LLMError("down", reason=LLMErrorReason.TIMEOUT)
    transport = _ScriptedTransport(error, _result("unused"))
    llm, sleeper = _llm(transport, max_attempts=1)

    with pytest.raises(LLMError):
        llm.complete(REQUEST)

    assert sleeper.delays == []


def test_empty_text_reports_empty_finish_reason():
    llm, _ = _llm(_ScriptedTransport(_result("   ")))

    assert llm.complete(REQUEST).finish_reason is FinishReason.EMPTY


def test_finish_reason_mapping():
    assert _finish_reason("stop") is FinishReason.STOP
    assert _finish_reason("length") is FinishReason.LENGTH
    assert _finish_reason("content_filter") is FinishReason.REFUSAL
    assert _finish_reason("tool_calls") is FinishReason.UNKNOWN
    assert _finish_reason(None) is FinishReason.UNKNOWN


def test_endpoint_defaults_to_the_public_base_url():
    assert _config(base_url=None).endpoint == (
        DEFAULT_BASE_URL + "/chat/completions"
    )
    assert _config(base_url="https://example.test/v1/").endpoint == (
        "https://example.test/v1/chat/completions"
    )


def test_missing_credentials_fail_before_any_connection(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    transport = OpenAICompatibleTransport()

    def _no_socket(*args, **kwargs):
        raise AssertionError("no socket may be opened")

    monkeypatch.setattr("socket.socket", _no_socket)

    with pytest.raises(LLMError) as excinfo:
        transport.complete(REQUEST, _config(api_key_env=API_KEY_ENV))

    assert excinfo.value.reason is LLMErrorReason.AUTH
    assert API_KEY_ENV in str(excinfo.value)


class _Body:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.test/v1/chat/completions", code, "error", {}, None
    )


def test_http_status_mapping_and_retry_classification(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "test-key")
    seen: list[urllib.request.Request] = []

    def _raise(code: int):
        def _urlopen(request, timeout):
            seen.append(request)
            raise _http_error(code)

        return _urlopen

    for code, expected in (
        (401, LLMErrorReason.AUTH),
        (403, LLMErrorReason.AUTH),
        (400, LLMErrorReason.RESPONSE),
        (429, LLMErrorReason.RATE_LIMIT),
        (500, LLMErrorReason.TRANSPORT),
    ):
        monkeypatch.setattr(urllib.request, "urlopen", _raise(code))
        transport = OpenAICompatibleTransport()
        with pytest.raises(LLMError) as excinfo:
            transport.complete(REQUEST, _config())
        assert excinfo.value.reason is expected, code

    sleeper = _Sleeper()
    llm = RealLLM(
        _config(max_attempts=2),
        transport=OpenAICompatibleTransport(),
        sleep=sleeper,
    )
    monkeypatch.setattr(urllib.request, "urlopen", _raise(403))
    with pytest.raises(LLMError):
        llm.complete(REQUEST)
    assert sleeper.delays == []


def test_invalid_response_bodies_are_response_errors(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "test-key")

    def _body(body: bytes):
        def _urlopen(request, timeout):
            return _Body(body)

        return _urlopen

    transport = OpenAICompatibleTransport()
    monkeypatch.setattr(urllib.request, "urlopen", _body(b"not-json"))
    with pytest.raises(LLMError) as excinfo:
        transport.complete(REQUEST, _config())
    assert excinfo.value.reason is LLMErrorReason.RESPONSE

    monkeypatch.setattr(
        urllib.request, "urlopen", _body(json.dumps({"choices": []}).encode())
    )
    with pytest.raises(LLMError) as excinfo:
        transport.complete(REQUEST, _config())
    assert excinfo.value.reason is LLMErrorReason.RESPONSE


def test_request_shape_carries_auth_but_responses_never_leak_it(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "secret-key-value")
    captured: dict[str, object] = {}

    def _urlopen(request, timeout):
        captured["request"] = request
        return _Body(
            json.dumps(
                {
                    "model": "server-model",
                    "choices": [
                        {
                            "message": {"content": '{"tests": []}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 2,
                        "total_tokens": 3,
                    },
                }
            ).encode()
        )

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    result = OpenAICompatibleTransport().complete(REQUEST, _config())

    outbound = captured["request"]
    assert isinstance(outbound, urllib.request.Request)
    assert outbound.get_header("Authorization") == "Bearer secret-key-value"
    assert "secret-key-value" not in result.text
    assert "secret-key-value" not in repr(result)


def test_read_api_key_reports_auth_without_leaking_the_value(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "  ")

    with pytest.raises(LLMError) as excinfo:
        read_api_key(_config(api_key_env=API_KEY_ENV))

    assert excinfo.value.reason is LLMErrorReason.AUTH


def test_configuration_rejects_out_of_range_attempts():
    with pytest.raises(ValueError):
        RealLLMConfig(model_id="model-x", max_attempts=0)
    with pytest.raises(ValueError):
        RealLLMConfig(model_id="model-x", max_attempts=4)


def test_fake_clients_never_open_sockets(monkeypatch):
    from llm.fake import DeterministicFakeLLM

    def _no_socket(*args, **kwargs):
        raise AssertionError("no socket may be opened")

    monkeypatch.setattr("socket.socket", _no_socket)
    monkeypatch.setattr("socket.create_connection", _no_socket)

    response = DeterministicFakeLLM().complete(REQUEST)

    assert response.model_kind.value == "fake"
