"""The single interface every model client implements."""

from __future__ import annotations

from typing import Protocol

from .models import LLMRequest, LLMResponse, ModelKind


class LLMClient(Protocol):
    model_kind: ModelKind
    model_id: str

    def complete(self, request: LLMRequest) -> LLMResponse:
        ...
