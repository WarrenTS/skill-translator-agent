import json

from translator_agent.models import ProviderUsage
from translator_agent.provider import ProviderError
from translator_agent.server import TranslatorRuntime


def request_payload(mode="translate_text", output=None):
    return {
        "schema_version": "translator-agent/v1",
        "request_id": "request-1",
        "session": {"id": "test-session", "context_policy": "isolated"},
        "mode": mode,
        "source": {"kind": "text", "locale": "zh-TW", "text": "你好 {name}"},
        "targets": ["en", "ja"],
        "output": output or {"format": "source", "path": None, "overwrite": False},
    }


def test_direct_translation_uses_one_call_and_no_auth(mock_provider, runtime_factory):
    base_url, handler = mock_provider
    config, assets = runtime_factory(base_url, auth_mode="none")
    runtime = TranslatorRuntime(config, assets)
    response = runtime.request(request_payload())
    assert response["status"] == "succeeded"
    assert response["metrics"]["model_calls"] == 1
    assert response["metrics"]["input_tokens"] == 10
    assert handler.authorization is None
    assert response["result"]["units"][0]["translations"]["en"]["text"] == "你好 {name} [en]"


def test_optional_auth_sends_header_only_when_key_exists(mock_provider, runtime_factory):
    base_url, handler = mock_provider
    config, assets = runtime_factory(base_url, auth_mode="optional", api_key="secret")
    runtime = TranslatorRuntime(config, assets)
    response = runtime.request(request_payload())
    assert response["status"] == "succeeded"
    assert handler.authorization == "Bearer secret"


def test_text_to_file_returns_artifact(mock_provider, runtime_factory):
    base_url, _handler = mock_provider
    config, assets = runtime_factory(base_url)
    runtime = TranslatorRuntime(config, assets)
    payload = request_payload(
        "translate_text_to_file",
        {"format": "json", "path": "translation.json", "overwrite": False},
    )
    response = runtime.request(payload)
    assert response["status"] == "succeeded"
    artifact = response["artifacts"][0]
    saved = config.output_root / artifact["path"]
    parsed = json.loads(saved.read_text())
    assert parsed["kind"] == "translation_bundle"
    assert len(artifact["sha256"]) == 64


def test_text_to_file_can_persist_complete_run_record(mock_provider, runtime_factory):
    base_url, _handler = mock_provider
    config, assets = runtime_factory(base_url)
    runtime = TranslatorRuntime(config, assets)
    payload = request_payload(
        "translate_text_to_file",
        {
            "format": "json",
            "path": "translation.json",
            "metrics_path": "translation.run.json",
            "overwrite": False,
        },
    )
    response = runtime.request(payload)
    assert response["status"] == "succeeded"
    assert len(response["artifacts"]) == 2
    record = json.loads((config.output_root / "translation.run.json").read_text())
    assert record["schema_version"] == "translator-agent/run-v1"
    assert record["provider"] == {"name": "custom", "model": "mock-flash"}
    assert record["metrics"]["input_tokens"] == 10
    assert record["metrics"]["output_tokens"] == 5
    assert record["metrics"]["total_tokens"] == 15
    assert record["metrics"]["usage_complete"] is True
    assert len(record["translation_artifacts"]) == 1


def test_session_mismatch_is_rejected_without_model_call(mock_provider, runtime_factory):
    base_url, handler = mock_provider
    config, assets = runtime_factory(base_url)
    runtime = TranslatorRuntime(config, assets)
    payload = request_payload()
    payload["session"]["id"] = "another-session"
    response = runtime.request(payload)
    assert response["status"] == "failed"
    assert response["errors"][0]["code"] == "SESSION_MISMATCH"
    assert handler.calls == 0


def test_invalid_provider_output_gets_one_bounded_repair(mock_provider, runtime_factory):
    base_url, _handler = mock_provider
    config, assets = runtime_factory(base_url)
    runtime = TranslatorRuntime(config, assets)
    valid_complete = runtime.graph.provider.complete
    calls = 0

    def flaky_complete(messages, *, timeout_seconds=None, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError(
                "provider returned non-JSON translation content",
                usage=ProviderUsage(
                    input_tokens=3,
                    output_tokens=2,
                    total_tokens=5,
                ),
            )
        return valid_complete(
            messages,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )

    runtime.graph.provider.complete = flaky_complete
    response = runtime.request(request_payload())

    assert response["status"] == "succeeded"
    assert response["metrics"]["model_calls"] == 2
    assert response["metrics"]["input_tokens"] == 13
    assert response["metrics"]["output_tokens"] == 7
    assert response["metrics"]["total_tokens"] == 20
    assert response["metrics"]["usage_reported_attempts"] == 2
    assert response["metrics"]["usage_missing_attempts"] == 0
    assert response["metrics"]["usage_complete"] is True
    assert response["warnings"][0]["code"] == "MODEL_OUTPUT_REPAIRED"


def test_missing_usage_is_not_reported_as_zero(mock_provider, runtime_factory):
    base_url, _handler = mock_provider
    config, assets = runtime_factory(base_url)
    runtime = TranslatorRuntime(config, assets)

    def failing_complete(_messages, *, timeout_seconds=None, **_kwargs):
        raise ProviderError("provider unavailable")

    runtime.graph.provider.complete = failing_complete
    response = runtime.request(request_payload())

    assert response["status"] == "failed"
    assert response["metrics"]["model_calls"] == 2
    assert response["metrics"]["input_tokens"] is None
    assert response["metrics"]["output_tokens"] is None
    assert response["metrics"]["usage_reported_attempts"] == 0
    assert response["metrics"]["usage_missing_attempts"] == 2
    assert response["metrics"]["usage_complete"] is False
