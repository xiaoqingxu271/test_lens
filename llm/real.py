"""Single real-provider adapter behind the provider-neutral LLM interface.

The first adapter speaks the OpenAI-compatible chat-completions shape. It uses
the standard library HTTP client, keeps TLS verification enabled, and never
logs credentials.
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Callable, Protocol

from pydantic import BaseModel, Field

from .errors import LLMError, LLMErrorReason
from .models import (
    FinishReason,
    LLMCallStats,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ModelKind,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
API_KEY_ENV = "TESTLENS_LLM_API_KEY"
BASE_URL_ENV = "TESTLENS_LLM_BASE_URL"
RETRY_DELAYS = (1.0, 2.0, 4.0)


class RealLLMConfig(BaseModel):
    model_id: str
    api_key_env: str = API_KEY_ENV
    base_url: str | None = None
    timeout_seconds: float = 60.0
    max_attempts: int = Field(3, ge=1, le=3)
    max_output_tokens: int | None = None
    temperature: float = 0.0

    @property
    def endpoint(self) -> str:
        return (self.base_url or DEFAULT_BASE_URL).rstrip("/") + "/chat/completions"


class ProviderResult(BaseModel):
    text: str
    finish_reason: FinishReason = FinishReason.STOP
    usage: LLMUsage = Field(default_factory=LLMUsage)
    model_id: str | None = None


class ProviderTransport(Protocol):
    def complete(
        self, request: LLMRequest, config: RealLLMConfig
    ) -> ProviderResult: ...


def read_api_key(config: RealLLMConfig) -> str:
    key = os.environ.get(config.api_key_env, "")
    if not key.strip():
        raise LLMError(
            f"missing model credential in {config.api_key_env}",
            reason=LLMErrorReason.AUTH,
        )
    return key


def _finish_reason(raw: object) -> FinishReason:
    return {
        "stop": FinishReason.STOP,
        "length": FinishReason.LENGTH,
        "content_filter": FinishReason.REFUSAL,
    }.get(raw, FinishReason.UNKNOWN)


class OpenAICompatibleTransport:
    """Minimal stdlib client for OpenAI-compatible chat completions."""

    def complete(
        self, request: LLMRequest, config: RealLLMConfig
    ) -> ProviderResult:
        api_key = read_api_key(config)
        body: dict[str, object] = {
            "model": config.model_id,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": config.temperature,
        }
        if config.max_output_tokens is not None:
            body["max_tokens"] = config.max_output_tokens
        if request.seed:
            body["seed"] = request.seed

        http_request = urllib.request.Request(
            config.endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                http_request, timeout=config.timeout_seconds
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                reason = LLMErrorReason.AUTH
            elif exc.code == 429:
                reason = LLMErrorReason.RATE_LIMIT
            elif 500 <= exc.code < 600:
                reason = LLMErrorReason.TRANSPORT
            else:
                reason = LLMErrorReason.RESPONSE
            raise LLMError(
                f"model request failed with HTTP {exc.code}",
                reason=reason,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LLMError(
                "model request timed out", reason=LLMErrorReason.TIMEOUT
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                reason = LLMErrorReason.TIMEOUT
            else:
                reason = LLMErrorReason.TRANSPORT
            raise LLMError(
                f"model transport error: {exc.reason}", reason=reason
            ) from exc
        except OSError as exc:
            raise LLMError(
                f"model transport error: {exc}",
                reason=LLMErrorReason.TRANSPORT,
            ) from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"invalid model response JSON: {exc.msg}",
                reason=LLMErrorReason.RESPONSE,
            ) from exc
        if not isinstance(payload, dict):
            raise LLMError(
                "invalid model response shape: expected a JSON object",
                reason=LLMErrorReason.RESPONSE,
            )

        try:
            choices = payload["choices"]
            choice = choices[0]
            message = choice["message"]
            text = message.get("content") or ""
            if not isinstance(text, str):
                raise TypeError("message content is not a string")
            usage_payload = payload.get("usage") or {}
            usage = LLMUsage(
                prompt_tokens=usage_payload.get("prompt_tokens"),
                completion_tokens=usage_payload.get("completion_tokens"),
                total_tokens=usage_payload.get("total_tokens"),
            )
            model_id = payload.get("model")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMError(
                f"invalid model response shape: {exc}",
                reason=LLMErrorReason.RESPONSE,
            ) from exc

        return ProviderResult(
            text=text,
            finish_reason=_finish_reason(choice.get("finish_reason")),
            usage=usage,
            model_id=model_id if isinstance(model_id, str) else None,
        )


class RealLLM:
    model_kind = ModelKind.REAL

    def __init__(
        self,
        config: RealLLMConfig,
        *,
        transport: ProviderTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.model_id = config.model_id
        self._transport = transport or OpenAICompatibleTransport()
        self._sleep = sleep
        self.stats = LLMCallStats()

    def complete(self, request: LLMRequest) -> LLMResponse:
        last_error: LLMError | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                result = self._transport.complete(request, self.config)
            except LLMError as exc:
                last_error = exc
                retryable = exc.reason in (
                    LLMErrorReason.TIMEOUT,
                    LLMErrorReason.RATE_LIMIT,
                    LLMErrorReason.TRANSPORT,
                )
                if not retryable or attempt >= self.config.max_attempts:
                    raise
                self.stats.retries += 1
                self._sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
                continue
            self.stats.calls += 1
            self.stats.prompt_tokens += result.usage.prompt_tokens or 0
            self.stats.completion_tokens += result.usage.completion_tokens or 0
            finish_reason = (
                FinishReason.EMPTY
                if not result.text.strip()
                else result.finish_reason
            )
            return LLMResponse(
                request_id=request.request_id,
                text=result.text,
                model_id=result.model_id or self.model_id,
                model_kind=ModelKind.REAL,
                finish_reason=finish_reason,
                usage=result.usage,
                attempts=attempt,
                replayed=False,
            )
        assert last_error is not None
        raise last_error
