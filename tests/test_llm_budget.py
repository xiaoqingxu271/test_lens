from __future__ import annotations

import pytest

from llm.budget import BudgetedLLM, LLMBudget
from llm.errors import LLMBudgetExceeded, LLMErrorReason
from llm.fake import DeterministicFakeLLM
from llm.models import (
    FinishReason,
    LLMCallStats,
    LLMPurpose,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ModelKind,
)


class _UsageInner:
    model_kind = ModelKind.REAL
    model_id = "usage-model"

    def __init__(self, total_tokens: int) -> None:
        self.total_tokens = total_tokens
        self.calls = 0
        self.stats = LLMCallStats()

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        self.stats.calls += 1
        return LLMResponse(
            request_id=request.request_id,
            text="ok",
            model_id=self.model_id,
            model_kind=ModelKind.REAL,
            finish_reason=FinishReason.STOP,
            usage=LLMUsage(
                prompt_tokens=self.total_tokens,
                completion_tokens=0,
                total_tokens=self.total_tokens,
            ),
        )


def _request(index: int = 0) -> LLMRequest:
    return LLMRequest(
        request_id=f"test_generation::item-{index}",
        purpose=LLMPurpose.TEST_GENERATION,
        prompt_template_version="generation-v1",
        system="system",
        prompt="prompt",
    )


def test_call_budget_is_checked_before_the_request():
    inner = DeterministicFakeLLM()
    client = BudgetedLLM(inner, LLMBudget(max_calls=1, max_total_tokens=None))

    client.complete(_request(0))

    with pytest.raises(LLMBudgetExceeded) as excinfo:
        client.complete(_request(1))

    assert excinfo.value.reason is LLMErrorReason.BUDGET
    assert len(inner.calls) == 1
    assert client.stats.calls == 1


def test_token_budget_is_checked_after_the_response():
    inner = _UsageInner(total_tokens=100)
    client = BudgetedLLM(inner, LLMBudget(max_calls=None, max_total_tokens=50))

    with pytest.raises(LLMBudgetExceeded):
        client.complete(_request())

    assert inner.calls == 1


def test_token_budget_accumulates_across_calls():
    inner = _UsageInner(total_tokens=30)
    client = BudgetedLLM(inner, LLMBudget(max_calls=None, max_total_tokens=50))

    client.complete(_request(0))

    with pytest.raises(LLMBudgetExceeded):
        client.complete(_request(1))

    assert inner.calls == 2


def test_missing_total_tokens_falls_back_to_the_parts():
    class _PartsInner(_UsageInner):
        def complete(self, request: LLMRequest) -> LLMResponse:
            response = super().complete(request)
            return response.model_copy(
                update={
                    "usage": LLMUsage(
                        prompt_tokens=40, completion_tokens=40, total_tokens=None
                    )
                }
            )

    client = BudgetedLLM(
        _PartsInner(0), LLMBudget(max_calls=None, max_total_tokens=50)
    )

    with pytest.raises(LLMBudgetExceeded):
        client.complete(_request())


def test_disabled_limits_allow_any_number_of_calls():
    inner = DeterministicFakeLLM()
    client = BudgetedLLM(
        inner, LLMBudget(max_calls=None, max_total_tokens=None)
    )

    for index in range(5):
        client.complete(_request(index))

    assert len(inner.calls) == 5


def test_budget_wrapper_preserves_model_identity_and_stats():
    inner = DeterministicFakeLLM()
    client = BudgetedLLM(inner)

    assert client.model_kind is inner.model_kind
    assert client.model_id == inner.model_id
    assert isinstance(client.stats, LLMCallStats)
    assert client.stats.calls == 0
