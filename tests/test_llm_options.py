from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from llm.budget import BudgetedLLM
from llm.models import (
    FinishReason,
    LLMCallStats,
    LLMPurpose,
    LLMRequest,
    LLMResponse,
    ModelKind,
)
from llm.options import (
    LLMMode,
    add_llm_arguments,
    build_llm,
    build_llm_factory,
    options_from_args,
)
from llm.real import BASE_URL_ENV, DEFAULT_BASE_URL
from llm.recording import RecordingLLM
from llm.replay import ReplayLLM


class _Inner:
    model_kind = ModelKind.REAL
    model_id = "recorded-model"

    def __init__(self) -> None:
        self.stats = LLMCallStats()

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.stats.calls += 1
        return LLMResponse(
            request_id=request.request_id,
            text="recorded",
            model_id=self.model_id,
            model_kind=ModelKind.REAL,
            finish_reason=FinishReason.STOP,
        )


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_llm_arguments(parser)
    return parser.parse_args(list(argv))


def _request(request_id: str = "test_generation::a") -> LLMRequest:
    return LLMRequest(
        request_id=request_id,
        purpose=LLMPurpose.TEST_GENERATION,
        prompt_template_version="generation-v1",
        system="system",
        prompt="prompt",
    )


def _transcript(tmp_path: Path) -> Path:
    path = tmp_path / "transcript.json"
    RecordingLLM(_Inner(), path).complete(_request())
    return path


def test_defaults_are_the_deterministic_fake():
    options = options_from_args(_parse())

    assert options.mode is LLMMode.FAKE
    assert options.max_calls == 200
    assert options.max_tokens == 500_000
    assert build_llm(options).model_kind is ModelKind.FAKE


def test_negative_budgets_are_rejected():
    with pytest.raises(ValueError):
        options_from_args(_parse("--max-calls", "-1"))
    with pytest.raises(ValueError):
        options_from_args(_parse("--max-tokens", "-1"))


@pytest.mark.parametrize(
    "argv",
    [
        ("--llm", "fake", "--transcript", "file.json"),
        ("--llm", "fake", "--record-to", "file.json"),
        ("--llm", "fake", "--model-id", "model"),
        ("--llm", "replay"),
        ("--llm", "replay", "--transcript", "file.json", "--record-to", "out.json"),
        ("--llm", "real"),
        ("--llm", "real", "--model-id", "model", "--transcript", "file.json"),
    ],
)
def test_invalid_option_combinations_are_rejected(argv):
    with pytest.raises(ValueError):
        options_from_args(_parse(*argv))


def test_replay_rejects_a_mismatched_expected_model(tmp_path):
    from llm.errors import ReplayError

    path = _transcript(tmp_path)
    options = options_from_args(
        _parse(
            "--llm",
            "replay",
            "--transcript",
            str(path),
            "--model-id",
            "another-model",
        )
    )

    with pytest.raises(ReplayError):
        build_llm(options)


def test_replay_builds_a_budgeted_replay_client(tmp_path):
    path = _transcript(tmp_path)
    options = options_from_args(
        _parse("--llm", "replay", "--transcript", str(path), "--max-calls", "5")
    )

    client = build_llm(options)

    assert isinstance(client, BudgetedLLM)
    assert isinstance(client._inner, ReplayLLM)
    assert client.model_kind is ModelKind.REAL
    response = client.complete(_request())
    assert response.replayed is True
    assert response.text == "recorded"


def test_real_builds_a_budgeted_client_and_reads_the_base_url_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(BASE_URL_ENV, "https://custom.test/v1")
    options = options_from_args(
        _parse("--llm", "real", "--model-id", "model-x")
    )

    client = build_llm(options)

    assert isinstance(client, BudgetedLLM)
    assert client.model_kind is ModelKind.REAL
    assert client.model_id == "model-x"
    assert client._inner.config.base_url == "https://custom.test/v1"


def test_real_without_base_url_env_uses_the_public_default(monkeypatch):
    monkeypatch.delenv(BASE_URL_ENV, raising=False)
    options = options_from_args(
        _parse("--llm", "real", "--model-id", "model-x")
    )

    client = build_llm(options)

    assert client._inner.config.base_url == DEFAULT_BASE_URL


def test_recording_wraps_the_real_client(tmp_path):
    options = options_from_args(
        _parse(
            "--llm",
            "real",
            "--model-id",
            "model-x",
            "--record-to",
            str(tmp_path / "recorded.json"),
        )
    )

    client = build_llm(options)

    assert isinstance(client._inner, RecordingLLM)
    assert (tmp_path / "recorded.json").exists()


def test_fake_factory_creates_a_new_fake_per_fixture():
    options = options_from_args(_parse())
    factory = build_llm_factory(options)

    first = factory(None)
    second = factory(None)

    assert first is not second
    assert first.model_kind is ModelKind.FAKE


def test_replay_factory_shares_one_inner_client_but_resets_budgets(tmp_path):
    path = _transcript(tmp_path)
    options = options_from_args(
        _parse(
            "--llm",
            "replay",
            "--transcript",
            str(path),
            "--max-calls",
            "1",
        )
    )
    factory = build_llm_factory(options)

    first = factory(None)
    second = factory(None)

    assert first is not second
    assert first._inner is second._inner
    assert first.complete(_request("test_generation::a")).text == "recorded"
    assert second.complete(_request("test_generation::a")).text == "recorded"
