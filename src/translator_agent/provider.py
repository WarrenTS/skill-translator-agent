from __future__ import annotations

import json
import http.client
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from translator_agent.config import RuntimeConfig
from translator_agent.models import ProviderResult, ProviderUsage
from translator_agent.security import redact


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        usage: ProviderUsage | None = None,
        attempt_usages: list[ProviderUsage] | None = None,
        model_calls: int = 1,
        provider_requests: int = 1,
        format_strategies: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage or ProviderUsage()
        self.attempt_usages = [self.usage] if attempt_usages is None else attempt_usages
        self.model_calls = model_calls
        self.provider_requests = provider_requests
        self.format_strategies = format_strategies or []


@dataclass
class OpenAICompatibleProvider:
    config: RuntimeConfig

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        timeout_seconds: int | None = None,
        max_output_tokens: int | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> ProviderResult:
        errors = self.config.validation_errors()
        if errors:
            raise ProviderError("; ".join(errors), model_calls=0, provider_requests=0)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        authorization = self.config.authorization_header()
        if authorization:
            headers["Authorization"] = authorization
        strategies = _format_strategies(self.config.response_format_strategy)
        attempt_usages: list[ProviderUsage] = []
        requested_strategies: list[str] = []
        provider_requests = 0
        for index, strategy in enumerate(strategies):
            requested_strategies.append(strategy)
            reasoning_override: str | None = None
            response_received = False
            while True:
                body = _request_body(
                    self.config,
                    messages,
                    strategy,
                    response_schema,
                    max_output_tokens,
                    reasoning_effort_override=reasoning_override,
                )
                request = urllib.request.Request(
                    self.config.chat_completions_url,
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                provider_requests += 1
                try:
                    with urllib.request.urlopen(
                        request, timeout=timeout_seconds or self.config.timeout_seconds
                    ) as response:
                        raw = response.read().decode("utf-8")
                    response_received = True
                    break
                except urllib.error.HTTPError as exc:
                    detail = exc.read(4096).decode("utf-8", errors="replace")
                    if (
                        self.config.provider == "openrouter"
                        and self.config.reasoning_effort == "none"
                        and reasoning_override is None
                        and _is_reasoning_required(exc.code, detail)
                    ):
                        reasoning_override = "minimal"
                        continue
                    if index + 1 < len(strategies) and _is_format_rejection(
                        exc.code, detail
                    ):
                        break
                    safe = redact(detail, [self.config.api_key])
                    raise ProviderError(
                        f"provider HTTP {exc.code}: {safe}",
                        attempt_usages=attempt_usages,
                        model_calls=len(attempt_usages),
                        provider_requests=provider_requests,
                        format_strategies=requested_strategies,
                    ) from exc
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    http.client.IncompleteRead,
                ) as exc:
                    safe = redact(str(exc), [self.config.api_key])
                    attempt_usages.append(ProviderUsage())
                    if isinstance(exc, http.client.IncompleteRead):
                        message = f"provider returned an incomplete response body: {safe}"
                    else:
                        message = f"provider request failed: {safe}"
                    raise ProviderError(
                        message,
                        attempt_usages=attempt_usages,
                        model_calls=len(attempt_usages),
                        provider_requests=provider_requests,
                        format_strategies=requested_strategies,
                    ) from exc
            if not response_received:
                continue

            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError as exc:
                attempt_usages.append(ProviderUsage())
                raise ProviderError(
                    "provider returned an invalid chat completion envelope",
                    attempt_usages=attempt_usages,
                    model_calls=len(attempt_usages),
                    provider_requests=provider_requests,
                    format_strategies=requested_strategies,
                ) from exc

            usage = _parse_usage(
                envelope.get("usage") if isinstance(envelope, dict) else None
            )
            attempt_usages.append(usage)
            try:
                choice = envelope["choices"][0]
                content = choice["message"]["content"]
                if isinstance(content, list):
                    content = "".join(
                        str(part.get("text", ""))
                        for part in content
                        if isinstance(part, dict)
                    )
            except (KeyError, IndexError, TypeError) as exc:
                raise ProviderError(
                    "provider returned an invalid chat completion envelope",
                    usage=usage,
                    attempt_usages=attempt_usages,
                    model_calls=len(attempt_usages),
                    provider_requests=provider_requests,
                    format_strategies=requested_strategies,
                ) from exc
            try:
                payload = _parse_json_content(str(content))
            except json.JSONDecodeError as exc:
                finish_reason = choice.get("finish_reason")
                if finish_reason == "length":
                    message = "provider output was truncated (finish_reason=length)"
                else:
                    message = "provider returned non-JSON translation content"
                raise ProviderError(
                    message,
                    usage=usage,
                    attempt_usages=attempt_usages,
                    model_calls=len(attempt_usages),
                    provider_requests=provider_requests,
                    format_strategies=requested_strategies,
                ) from exc
            return ProviderResult(
                payload=payload,
                usage=_sum_usage(attempt_usages),
                attempt_usages=attempt_usages,
                model_calls=len(attempt_usages),
                provider_requests=provider_requests,
                format_strategies=requested_strategies,
            )

        raise ProviderError("provider formatting strategies were exhausted")


def _format_strategies(configured: str) -> list[str]:
    if configured == "auto":
        return ["json_schema", "json_object", "prompt"]
    return [configured]


def _request_body(
    config: RuntimeConfig,
    messages: list[dict[str, str]],
    strategy: str,
    response_schema: dict[str, Any] | None,
    max_output_tokens: int | None,
    reasoning_effort_override: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "stream": False,
        "temperature": 0.1,
    }
    if max_output_tokens:
        body["max_tokens"] = max_output_tokens
    if strategy == "json_schema" and response_schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "translation_result",
                "strict": True,
                "schema": response_schema,
            },
        }
        if config.provider == "openrouter":
            body["provider"] = {"require_parameters": True}
    elif strategy == "json_object":
        body["response_format"] = {"type": "json_object"}
    if config.provider == "openrouter":
        reasoning: dict[str, Any] = {"exclude": True}
        reasoning_effort = reasoning_effort_override or config.reasoning_effort
        if reasoning_effort != "auto":
            reasoning["effort"] = reasoning_effort
        body["reasoning"] = reasoning
    return body


def _is_format_rejection(status: int, detail: str) -> bool:
    if status not in {400, 404, 422}:
        return False
    lowered = detail.lower()
    markers = (
        "response_format",
        "json_schema",
        "structured output",
        "unsupported parameter",
        "requested parameters",
    )
    return any(marker in lowered for marker in markers)


def _is_reasoning_required(status: int, detail: str) -> bool:
    if status not in {400, 422}:
        return False
    lowered = detail.lower()
    return "reasoning" in lowered and any(
        marker in lowered
        for marker in ("mandatory", "cannot be disabled", "is required")
    )


def _sum_usage(items: list[ProviderUsage]) -> ProviderUsage:
    def total(field: str) -> int | None:
        values = [getattr(item, field) for item in items]
        present = [value for value in values if value is not None]
        return sum(present) if present else None

    costs = [item.provider_cost for item in items if item.provider_cost is not None]
    return ProviderUsage(
        input_tokens=total("input_tokens"),
        output_tokens=total("output_tokens"),
        total_tokens=total("total_tokens"),
        cached_input_tokens=total("cached_input_tokens"),
        reasoning_tokens=total("reasoning_tokens"),
        provider_cost=sum(costs) if costs else None,
    )


def _parse_usage(raw: Any) -> ProviderUsage:
    if not isinstance(raw, dict):
        return ProviderUsage()
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    return ProviderUsage(
        input_tokens=_first_int(raw, "prompt_tokens", "input_tokens"),
        output_tokens=_first_int(raw, "completion_tokens", "output_tokens"),
        total_tokens=_first_int(raw, "total_tokens"),
        cached_input_tokens=_first_int(prompt_details, "cached_tokens"),
        reasoning_tokens=_first_int(completion_details, "reasoning_tokens"),
        provider_cost=_first_number(raw, "cost"),
    )


def _first_int(mapping: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _first_number(mapping: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return None


def _parse_json_content(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as original:
        decoder = json.JSONDecoder()
        fallback: dict[str, Any] | None = None
        for start, character in enumerate(stripped):
            if character != "{":
                continue
            try:
                candidate, _end = decoder.raw_decode(stripped[start:])
            except json.JSONDecodeError:
                continue
            if not isinstance(candidate, dict):
                continue
            if "translations" in candidate:
                parsed = candidate
                break
            if fallback is None:
                fallback = candidate
        else:
            if fallback is None:
                raise original
            parsed = fallback
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("expected JSON object", stripped, 0)
    return parsed
