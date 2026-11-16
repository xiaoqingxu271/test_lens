"""Per-run call and token budgets for model clients."""

from __future__ import annotations

from pydantic import BaseModel

from .errors import LLMBudgetExceeded
from .models import LLMCallStats, LLMRequest, LLMResponse


class LLMBudget(BaseModel):
    max_calls: int | None = 200
    max_total_tokens: int | None = 500_000


class BudgetedLLM:
    def __init__(self, inner, budget: LLMBudget | None = None) -> None:
        self._inner = inner
        self.budget = budget or LLMBudget()
        self.model_kind = inner.model_kind
        self.model_id = inner.model_id
        self._calls = 0
        self._total_tokens = 0

    @property
    def stats(self) -> LLMCallStats:
        stats = getattr(self._inner, "stats", None)
        return stats if isinstance(stats, LLMCallStats) else LLMCallStats()

    def complete(self, request: LLMRequest) -> LLMResponse:
        maximum = self.budget.max_calls
        if maximum is not None and self._calls >= maximum:
            raise LLMBudgetExceeded(
                f"model call budget exhausted: {self._calls}/{maximum}"
            )
        response = self._inner.complete(request)
        self._calls += 1

        total = response.usage.total_tokens
        if total is None:
            prompt = response.usage.prompt_tokens or 0
            completion = response.usage.completion_tokens or 0
            total = prompt + completion if (prompt or completion) else 0
        self._total_tokens += total
        token_limit = self.budget.max_total_tokens
        if token_limit is not None and self._total_tokens > token_limit:
            raise LLMBudgetExceeded(
                f"model token budget exceeded: "
                f"{self._total_tokens}/{token_limit}"
            )
        return response
