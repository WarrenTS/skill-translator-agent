from __future__ import annotations

import json
import io
import http.client
from types import SimpleNamespace
import urllib.error

import pytest

from translator_agent.provider import (
    OpenAICompatibleProvider,
    ProviderError,
    _format_strategies,
    _parse_json_content,
    _request_body,
)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_malformed_content_preserves_endpoint_usage(monkeypatch):
    envelope = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "not valid JSON"},
            }
        ],
        "usage": {
            "prompt_tokens": 101,
            "completion_tokens": 17,
            "total_tokens": 118,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 3},
            "cost": 0.000123,
        },
    }
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(envelope),
    )
    config = SimpleNamespace(
        validation_errors=lambda: [],
        model="test-model",
        authorization_header=lambda: None,
        chat_completions_url="https://example.invalid/v1/chat/completions",
        timeout_seconds=5,
        api_key="",
        provider="custom",
        response_format_strategy="prompt",
    )
    provider = OpenAICompatibleProvider(config)

    with pytest.raises(ProviderError) as caught:
        provider.complete([{"role": "user", "content": "translate"}])

    assert caught.value.usage.input_tokens == 101
    assert caught.value.usage.output_tokens == 17
    assert caught.value.usage.total_tokens == 118
    assert caught.value.usage.cached_input_tokens == 40
    assert caught.value.usage.reasoning_tokens == 3
    assert caught.value.usage.provider_cost == pytest.approx(0.000123)


def test_parser_finds_translation_object_after_reasoning_markup():
    content = (
        '<think>Use a mapping such as {"locale": "zh-TW"}.</think>\n'
        '{"detected_source_locale":"en","translations":[]}'
        "\nTranslation complete."
    )

    parsed = _parse_json_content(content)

    assert parsed["detected_source_locale"] == "en"
    assert parsed["translations"] == []


def test_openrouter_strict_body_has_schema_budget_and_clean_reasoning():
    config = SimpleNamespace(
        model="model-id", provider="openrouter", reasoning_effort="none"
    )
    schema = {"type": "object", "properties": {}}

    body = _request_body(
        config,
        [{"role": "user", "content": "translate"}],
        "json_schema",
        schema,
        4096,
    )

    assert body["max_tokens"] == 4096
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["provider"] == {"require_parameters": True}
    assert body["reasoning"] == {"exclude": True, "effort": "none"}
    assert _format_strategies("auto") == ["json_schema", "json_object", "prompt"]


def test_auto_format_falls_back_after_unsupported_schema(monkeypatch):
    calls = 0
    envelope = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": '{"detected_source_locale":"en","translations":[]}'
                },
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }

    def urlopen(request, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "bad request",
                {},
                io.BytesIO(b"response_format json_schema is unsupported"),
            )
        return FakeResponse(envelope)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    config = SimpleNamespace(
        validation_errors=lambda: [],
        model="test-model",
        authorization_header=lambda: None,
        chat_completions_url="https://example.invalid/v1/chat/completions",
        timeout_seconds=5,
        api_key="",
        provider="custom",
        response_format_strategy="auto",
    )

    result = OpenAICompatibleProvider(config).complete(
        [{"role": "user", "content": "translate"}],
        response_schema={"type": "object", "properties": {}},
    )

    assert result.provider_requests == 2
    assert result.model_calls == 1
    assert result.format_strategies == ["json_schema", "json_object"]
    assert result.usage.total_tokens == 20


def test_openrouter_retries_with_minimal_when_reasoning_is_mandatory(monkeypatch):
    efforts = []
    envelope = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": '{"detected_source_locale":"en","translations":[]}'
                },
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
        },
    }

    def urlopen(request, **_kwargs):
        body = json.loads(request.data)
        efforts.append(body["reasoning"]["effort"])
        if len(efforts) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "bad request",
                {},
                io.BytesIO(b"Reasoning is mandatory and cannot be disabled."),
            )
        return FakeResponse(envelope)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    config = SimpleNamespace(
        validation_errors=lambda: [],
        model="test-model",
        authorization_header=lambda: None,
        chat_completions_url="https://example.invalid/v1/chat/completions",
        timeout_seconds=5,
        api_key="",
        provider="openrouter",
        response_format_strategy="json_schema",
        reasoning_effort="none",
    )

    result = OpenAICompatibleProvider(config).complete(
        [{"role": "user", "content": "translate"}],
        response_schema={"type": "object", "properties": {}},
    )

    assert efforts == ["none", "minimal"]
    assert result.provider_requests == 2
    assert result.model_calls == 1
    assert result.usage.total_tokens == 20


def test_incomplete_http_body_becomes_retryable_provider_error(monkeypatch):
    class IncompleteResponse(FakeResponse):
        def read(self) -> bytes:
            raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: IncompleteResponse({}),
    )
    config = SimpleNamespace(
        validation_errors=lambda: [],
        model="test-model",
        authorization_header=lambda: None,
        chat_completions_url="https://example.invalid/v1/chat/completions",
        timeout_seconds=5,
        api_key="",
        provider="custom",
        response_format_strategy="prompt",
    )

    with pytest.raises(ProviderError) as caught:
        OpenAICompatibleProvider(config).complete(
            [{"role": "user", "content": "translate"}]
        )

    assert "incomplete response body" in str(caught.value)
    assert caught.value.model_calls == 1
    assert caught.value.provider_requests == 1
    assert caught.value.attempt_usages[0].input_tokens is None


def test_auto_format_does_not_downgrade_after_generated_malformed_content(monkeypatch):
    envelope = {
        "choices": [{"finish_reason": "stop", "message": {"content": "broken"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    }
    calls = 0

    def urlopen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse(envelope)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    config = SimpleNamespace(
        validation_errors=lambda: [],
        model="test-model",
        authorization_header=lambda: None,
        chat_completions_url="https://example.invalid/v1/chat/completions",
        timeout_seconds=5,
        api_key="",
        provider="custom",
        response_format_strategy="auto",
    )

    with pytest.raises(ProviderError) as caught:
        OpenAICompatibleProvider(config).complete(
            [{"role": "user", "content": "translate"}],
            response_schema={"type": "object", "properties": {}},
        )

    assert calls == 1
    assert caught.value.model_calls == 1
    assert caught.value.format_strategies == ["json_schema"]
    assert caught.value.usage.total_tokens == 13
