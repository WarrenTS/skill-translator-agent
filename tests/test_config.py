from pathlib import Path

from translator_agent.config import RuntimeConfig


def make_config(**overrides):
    values = {
        "provider": "custom",
        "base_url": "http://localhost:8000/v1",
        "api_key": "",
        "model": "flash-model",
        "auth_mode": "none",
        "allow_insecure_http": True,
        "timeout_seconds": 60,
        "max_retries": 1,
        "max_batch_chars": 12000,
        "max_output_tokens": 8192,
        "response_format_strategy": "auto",
        "reasoning_effort": "none",
        "idle_timeout_seconds": 900,
        "input_root": Path("/work/input"),
        "output_root": Path("/work/output"),
        "socket_path": Path("/run/agent.sock"),
        "config_dir": Path("/app/config"),
        "session_id": "session",
        "cost_profile": "flash",
    }
    values.update(overrides)
    return RuntimeConfig(**values)


def test_no_auth_accepts_blank_key_and_omits_header():
    config = make_config(auth_mode="none", api_key="should-not-be-used")
    assert config.validation_errors() == []
    assert config.authorization_header() is None


def test_optional_auth_accepts_blank_key():
    config = make_config(auth_mode="optional", api_key="")
    assert config.validation_errors() == []
    assert config.authorization_header() is None


def test_required_auth_rejects_blank_key():
    config = make_config(auth_mode="required", api_key="")
    assert "TRANSLATOR_API_KEY is required" in config.validation_errors()


def test_known_provider_cannot_disable_required_auth():
    config = make_config(provider="openai", auth_mode="none")
    assert any("openai preset requires" in item for item in config.validation_errors())


def test_public_http_requires_explicit_opt_in():
    config = make_config(allow_insecure_http=False)
    assert any("ALLOW_INSECURE_HTTP" in item for item in config.validation_errors())


def test_template_has_no_secret_and_all_required_fields():
    template = (Path(__file__).parents[1] / "assets" / "transenv.template").read_text()
    for key in (
        "TRANSLATOR_PROVIDER=",
        "TRANSLATOR_API_BASE_URL=",
        "TRANSLATOR_MODEL=",
        "TRANSLATOR_AUTH_MODE=",
        "TRANSLATOR_API_KEY=",
    ):
        assert key in template
    assert "sk-" not in template
