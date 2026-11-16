"""CLI-facing model selection and client construction."""

from __future__ import annotations

import argparse
import os
from enum import Enum
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel

from .budget import BudgetedLLM, LLMBudget
from .fake import DeterministicFakeLLM
from .real import (
    API_KEY_ENV,
    BASE_URL_ENV,
    DEFAULT_BASE_URL,
    RealLLM,
    RealLLMConfig,
)
from .recording import RecordingLLM
from .replay import ReplayLLM


class LLMMode(str, Enum):
    FAKE = "fake"
    REPLAY = "replay"
    REAL = "real"


class LLMOptions(BaseModel):
    mode: LLMMode = LLMMode.FAKE
    transcript: Path | None = None
    record_to: Path | None = None
    model_id: str | None = None
    max_calls: int | None = 200
    max_tokens: int | None = 500_000
    timeout_seconds: float = 60.0
    max_attempts: int = 3
    api_key_env: str = API_KEY_ENV
    base_url: str | None = None


def add_llm_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--llm",
        choices=[item.value for item in LLMMode],
        default=LLMMode.FAKE.value,
        help="model source; fake is the deterministic default",
    )
    parser.add_argument(
        "--transcript",
        default=None,
        help="transcript to replay; requires --llm replay",
    )
    parser.add_argument(
        "--record-to",
        default=None,
        help="transcript path for a real run; requires --llm real",
    )
    parser.add_argument(
        "--model-id",
        default=None,
        help="provider model id; required for --llm real",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=200,
        help="per-run model call budget",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=500_000,
        help="per-run model token budget",
    )


def options_from_args(args: argparse.Namespace) -> LLMOptions:
    mode = LLMMode(args.llm)
    transcript = Path(args.transcript) if args.transcript else None
    record_to = Path(args.record_to) if args.record_to else None
    model_id = args.model_id
    if mode is LLMMode.FAKE:
        if transcript or record_to or model_id:
            raise ValueError(
                "--transcript, --record-to, and --model-id require "
                "--llm replay or --llm real"
            )
    elif mode is LLMMode.REPLAY:
        if transcript is None:
            raise ValueError("--llm replay requires --transcript")
        if record_to is not None:
            raise ValueError("--record-to requires --llm real")
    else:
        if transcript is not None:
            raise ValueError("--transcript requires --llm replay")
        if not model_id:
            raise ValueError("--llm real requires --model-id")
    if args.max_calls < 0:
        raise ValueError("--max-calls must be zero or greater")
    if args.max_tokens < 0:
        raise ValueError("--max-tokens must be zero or greater")
    return LLMOptions(
        mode=mode,
        transcript=transcript,
        record_to=record_to,
        model_id=model_id,
        max_calls=args.max_calls,
        max_tokens=args.max_tokens,
        base_url=os.environ.get(BASE_URL_ENV) or None,
    )


def _budget(options: LLMOptions) -> LLMBudget:
    return LLMBudget(
        max_calls=options.max_calls,
        max_total_tokens=options.max_tokens,
    )


def _inner_llm(options: LLMOptions):
    """The unbudgeted client; budget wrappers are per stage invocation."""
    if options.mode is LLMMode.REPLAY:
        assert options.transcript is not None
        return ReplayLLM(
            options.transcript,
            expected_model_id=options.model_id,
        )

    assert options.model_id is not None
    config = RealLLMConfig(
        model_id=options.model_id,
        api_key_env=options.api_key_env,
        base_url=options.base_url or DEFAULT_BASE_URL,
        timeout_seconds=options.timeout_seconds,
        max_attempts=options.max_attempts,
    )
    inner = RealLLM(config)
    if options.record_to is not None:
        inner = RecordingLLM(inner, options.record_to)
    return inner


def build_llm(
    options: LLMOptions,
    responses: Mapping[str, str] | None = None,
):
    if options.mode is LLMMode.FAKE:
        return DeterministicFakeLLM(responses)
    return BudgetedLLM(_inner_llm(options), _budget(options))


def build_llm_factory(options: LLMOptions):
    """Return a factory used by the evaluation harness.

    Fake mode needs per-fixture registered responses, so it builds a new fake
    each time. Replay and real modes share one inner client for the whole run
    (recording appends to one transcript, replay validates against one
    transcript) while each stage invocation gets a fresh budget wrapper.
    """
    if options.mode is LLMMode.FAKE:
        return lambda responses=None: DeterministicFakeLLM(responses)
    inner = _inner_llm(options)
    return lambda responses=None: BudgetedLLM(inner, _budget(options))
