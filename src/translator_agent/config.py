from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml


PROVIDER_PRESETS = {
    "openai": ("https://api.openai.com/v1", "required"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "required"),
    "openrouter": ("https://openrouter.ai/api/v1", "required"),
}


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class RuntimeConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    auth_mode: str
    allow_insecure_http: bool
    timeout_seconds: int
    max_retries: int
    max_batch_chars: int
    max_output_tokens: int
    response_format_strategy: str
    reasoning_effort: str
    idle_timeout_seconds: int
    input_root: Path
    output_root: Path
    socket_path: Path
    config_dir: Path
    session_id: str
    cost_profile: str

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        provider = os.getenv("TRANSLATOR_PROVIDER", "custom").strip().lower()
        preset_url, preset_auth = PROVIDER_PRESETS.get(provider, ("", "optional"))
        base_url = os.getenv("TRANSLATOR_API_BASE_URL", preset_url).strip().rstrip("/")
        auth_mode = os.getenv("TRANSLATOR_AUTH_MODE", preset_auth).strip().lower()
        session_id = os.getenv("TRANSLATOR_SESSION_ID", "").strip() or uuid.uuid4().hex
        return cls(
            provider=provider,
            base_url=base_url,
            api_key=os.getenv("TRANSLATOR_API_KEY", "").strip(),
            model=os.getenv("TRANSLATOR_MODEL", "").strip(),
            auth_mode=auth_mode,
            allow_insecure_http=_bool_env("TRANSLATOR_ALLOW_INSECURE_HTTP"),
            timeout_seconds=_int_env("TRANSLATOR_TIMEOUT_SECONDS", 60, 1, 600),
            max_retries=_int_env("TRANSLATOR_MAX_RETRIES", 1, 0, 1),
            max_batch_chars=_int_env("TRANSLATOR_MAX_BATCH_CHARS", 12000, 256, 200000),
            max_output_tokens=_int_env(
                "TRANSLATOR_MAX_OUTPUT_TOKENS", 8192, 256, 131072
            ),
            response_format_strategy=os.getenv(
                "TRANSLATOR_RESPONSE_FORMAT", "auto"
            ).strip().lower(),
            reasoning_effort=os.getenv(
                "TRANSLATOR_REASONING_EFFORT", "none"
            ).strip().lower(),
            idle_timeout_seconds=_int_env(
                "TRANSLATOR_IDLE_TIMEOUT_SECONDS", 900, 30, 86400
            ),
            input_root=Path(os.getenv("TRANSLATOR_INPUT_ROOT", "/work/input")),
            output_root=Path(os.getenv("TRANSLATOR_OUTPUT_ROOT", "/work/output")),
            socket_path=Path(
                os.getenv(
                    "TRANSLATOR_SOCKET_PATH", "/run/translator-agent/agent.sock"
                )
            ),
            config_dir=Path(os.getenv("TRANSLATOR_CONFIG_DIR", "/app/config")),
            session_id=session_id,
            cost_profile=os.getenv("TRANSLATOR_COST_PROFILE", "flash").strip().lower(),
        )

    def validation_errors(self) -> list[str]:
        errors: list[str] = []
        if self.provider not in {"openai", "nvidia", "openrouter", "custom"}:
            errors.append("TRANSLATOR_PROVIDER is unsupported")
        if self.auth_mode not in {"required", "optional", "none"}:
            errors.append("TRANSLATOR_AUTH_MODE must be required, optional, or none")
        if self.provider in PROVIDER_PRESETS and self.auth_mode != "required":
            errors.append(f"{self.provider} preset requires TRANSLATOR_AUTH_MODE=required")
        if not self.base_url:
            errors.append("TRANSLATOR_API_BASE_URL is missing")
        else:
            parsed = urlparse(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append("TRANSLATOR_API_BASE_URL must be an absolute HTTP(S) URL")
            if parsed.username or parsed.password:
                errors.append("TRANSLATOR_API_BASE_URL must not contain credentials")
            if parsed.scheme == "http" and not self.allow_insecure_http:
                errors.append(
                    "HTTP endpoint requires TRANSLATOR_ALLOW_INSECURE_HTTP=true"
                )
        if not self.model:
            errors.append("TRANSLATOR_MODEL is missing")
        if self.auth_mode == "required" and not self.api_key:
            errors.append("TRANSLATOR_API_KEY is required")
        if self.cost_profile != "flash":
            errors.append("TRANSLATOR_COST_PROFILE must be flash")
        if self.response_format_strategy not in {
            "auto",
            "json_schema",
            "json_object",
            "prompt",
        }:
            errors.append(
                "TRANSLATOR_RESPONSE_FORMAT must be auto, json_schema, json_object, or prompt"
            )
        if self.reasoning_effort not in {
            "auto",
            "none",
            "minimal",
            "low",
            "medium",
            "high",
        }:
            errors.append(
                "TRANSLATOR_REASONING_EFFORT must be auto, none, minimal, low, medium, or high"
            )
        return errors

    @property
    def provider_ready(self) -> bool:
        return not self.validation_errors()

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def authorization_header(self) -> str | None:
        if self.auth_mode == "none" or not self.api_key:
            return None
        return f"Bearer {self.api_key}"


@dataclass(frozen=True)
class AgentAssets:
    context_version: str
    system_prompt: str
    tools_version: str
    tool_ids: tuple[str, ...]

    @classmethod
    def load(cls, config_dir: Path) -> "AgentAssets":
        context = yaml.safe_load((config_dir / "agent-context.yaml").read_text())
        tools = yaml.safe_load((config_dir / "tools.yaml").read_text())
        if not isinstance(context, dict) or not context.get("system"):
            raise ValueError("agent-context.yaml is invalid")
        if not isinstance(tools, dict) or tools.get("model_callable") is not False:
            raise ValueError("tools.yaml must disable model-callable tools")
        tool_ids = tools.get("tools")
        if not isinstance(tool_ids, list) or not all(
            isinstance(item, str) for item in tool_ids
        ):
            raise ValueError("tools.yaml tools must be a string list")
        return cls(
            context_version=str(context.get("version", "unknown")),
            system_prompt=" ".join(str(context["system"]).split()),
            tools_version=str(tools.get("version", "unknown")),
            tool_ids=tuple(tool_ids),
        )
