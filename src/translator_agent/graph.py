from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from translator_agent.adapters import Extraction, extract_source, render_output
from translator_agent.config import AgentAssets, RuntimeConfig
from translator_agent.models import ExtractedUnit, ProviderResult, ProviderUsage, TranslationRequest
from translator_agent.prompts import build_messages, translation_response_schema
from translator_agent.provider import OpenAICompatibleProvider, ProviderError
from translator_agent.security import artifact_record, atomic_write, resolve_within
from translator_agent.session import SessionState
from translator_agent.timeouts import (
    completion_token_budget,
    effective_batch_chars,
    provider_timeout_seconds,
)
from translator_agent.validators import protected_tokens, validate_target


class GraphState(TypedDict, total=False):
    request: TranslationRequest
    request_sequence: int
    extraction: Extraction
    resolved_source_locale: str
    translated: dict[str, dict[str, str]]
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    provider_calls: int
    provider_requests: int
    format_strategy_counts: dict[str, int]
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    provider_cost: float
    usage_reported_attempts: int
    usage_missing_attempts: int
    cached_input_reported_attempts: int
    reasoning_reported_attempts: int
    provider_cost_reported_attempts: int
    result: dict[str, Any]


class TranslatorGraph:
    def __init__(
        self,
        config: RuntimeConfig,
        assets: AgentAssets,
        session: SessionState,
    ) -> None:
        self.config = config
        self.assets = assets
        self.session = session
        self.provider = OpenAICompatibleProvider(config)
        builder = StateGraph(GraphState)
        builder.add_node("extract", self._extract)
        builder.add_node("translate", self._translate)
        builder.add_node("finalize", self._finalize)
        builder.add_edge(START, "extract")
        builder.add_edge("extract", "translate")
        builder.add_edge("translate", "finalize")
        builder.add_edge("finalize", END)
        self.compiled = builder.compile()

    def invoke(self, request: TranslationRequest, request_sequence: int) -> dict[str, Any]:
        state = self.compiled.invoke(
            {"request": request, "request_sequence": request_sequence}
        )
        return self._response(state)

    def _extract(self, state: GraphState) -> GraphState:
        try:
            extraction = extract_source(state["request"], self.config)
            if not extraction.units:
                raise ValueError("source contains no translatable units")
            return {
                "extraction": extraction,
                "errors": [],
                "warnings": [],
                "artifacts": [],
                "provider_calls": 0,
                "provider_requests": 0,
                "format_strategy_counts": {},
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
                "provider_cost": 0.0,
                "usage_reported_attempts": 0,
                "usage_missing_attempts": 0,
                "cached_input_reported_attempts": 0,
                "reasoning_reported_attempts": 0,
                "provider_cost_reported_attempts": 0,
            }
        except Exception as exc:
            return {
                "errors": [
                    {
                        "code": "SOURCE_ERROR",
                        "message": str(exc),
                        "retryable": False,
                    }
                ],
                "warnings": [],
                "artifacts": [],
            }

    def _translate(self, state: GraphState) -> GraphState:
        if state.get("errors"):
            return {}
        request = state["request"]
        extraction = state["extraction"]
        batch_limit = effective_batch_chars(
            self.config.max_batch_chars,
            len(request.targets),
        )
        batches = _batch_units(extraction.units, batch_limit)
        translated: dict[str, dict[str, str]] = {}
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = list(state.get("warnings", []))
        provider_calls = 0
        provider_requests = 0
        format_strategy_counts: dict[str, int] = {}
        usage_totals: dict[str, int | float] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "provider_cost": 0.0,
            "usage_reported_attempts": 0,
            "usage_missing_attempts": 0,
            "cached_input_reported_attempts": 0,
            "reasoning_reported_attempts": 0,
            "provider_cost_reported_attempts": 0,
        }
        resolved_locale = request.source.locale if request.source.locale != "auto" else "und"

        def provider_attempt(
            messages: list[dict[str, str]], timeout_seconds: int, source_chars: int
        ) -> ProviderResult:
            nonlocal provider_calls, provider_requests
            try:
                result = self.provider.complete(
                    messages,
                    timeout_seconds=timeout_seconds,
                    max_output_tokens=completion_token_budget(
                        source_chars,
                        len(request.targets),
                        self.config.max_output_tokens,
                    ),
                    response_schema=translation_response_schema(request.targets),
                )
            except ProviderError as exc:
                provider_calls += exc.model_calls
                provider_requests += exc.provider_requests
                for strategy in exc.format_strategies:
                    format_strategy_counts[strategy] = (
                        format_strategy_counts.get(strategy, 0) + 1
                    )
                for usage in exc.attempt_usages:
                    _accumulate_usage(usage_totals, usage)
                raise
            provider_calls += result.model_calls
            provider_requests += result.provider_requests
            for strategy in result.format_strategies:
                format_strategy_counts[strategy] = (
                    format_strategy_counts.get(strategy, 0) + 1
                )
            for usage in result.attempt_usages or [result.usage]:
                _accumulate_usage(usage_totals, usage)
            return result

        for batch in batches:
            batch_timeout = provider_timeout_seconds(
                self.config.timeout_seconds,
                sum(len(item.text) for item in batch),
                len(request.targets),
            )
            memory = self.session.relevant_memory(
                [item.model_dump() for item in batch],
                request.targets,
                request.session.context_policy == "continue",
            )
            messages = build_messages(self.assets.system_prompt, request, batch, memory)
            repair_used = False
            try:
                result = provider_attempt(
                    messages,
                    batch_timeout,
                    sum(len(item.text) for item in batch),
                )
                batch_translated = _normalize_translations(
                    result.payload, batch, request.targets
                )
            except (ProviderError, ValueError) as first_exc:
                if not self.config.max_retries:
                    errors.extend(_model_output_errors(batch, request.targets, first_exc))
                    continue
                repair_used = True
                retry_messages = _add_repair_instruction(
                    messages,
                    {
                        "reason": str(first_exc),
                        "instruction": "Return exactly the requested JSON object and no other text.",
                    },
                )
                try:
                    result = provider_attempt(
                        retry_messages,
                        batch_timeout,
                        sum(len(item.text) for item in batch),
                    )
                    batch_translated = _normalize_translations(
                        result.payload, batch, request.targets
                    )
                    warnings.append(
                        {
                            "code": "MODEL_OUTPUT_REPAIRED",
                            "message": "A malformed provider response was repaired with one bounded retry.",
                            "unit_ids": [unit.id for unit in batch],
                        }
                    )
                except (ProviderError, ValueError) as retry_exc:
                    errors.extend(
                        _model_output_errors(batch, request.targets, retry_exc)
                    )
                    continue

            detected = result.payload.get("detected_source_locale")
            if resolved_locale == "und" and isinstance(detected, str) and detected:
                resolved_locale = detected

            failed_units: dict[str, list[dict[str, Any]]] = {}
            for unit in batch:
                for locale in request.targets:
                    findings = validate_target(
                        unit.text, batch_translated[unit.id][locale]
                    )
                    if findings:
                        failed_units.setdefault(unit.id, []).extend(
                            {**finding, "target_locale": locale}
                            for finding in findings
                        )

            if failed_units and self.config.max_retries and not repair_used:
                retry_units = [unit for unit in batch if unit.id in failed_units]
                retry_messages = build_messages(
                    self.assets.system_prompt, request, retry_units, memory=[]
                )
                retry_messages = _add_repair_instruction(
                    retry_messages,
                    failed_units,
                )
                try:
                    retry_result = provider_attempt(
                        retry_messages,
                        batch_timeout,
                        sum(len(item.text) for item in retry_units),
                    )
                    repaired = _normalize_translations(
                        retry_result.payload, retry_units, request.targets
                    )
                    batch_translated.update(repaired)
                except (ProviderError, ValueError) as exc:
                    warnings.append(
                        {
                            "code": "REPAIR_FAILED",
                            "message": str(exc),
                            "unit_ids": sorted(failed_units),
                        }
                    )

            for unit in batch:
                unit_failed = False
                for locale in request.targets:
                    findings = validate_target(
                        unit.text, batch_translated[unit.id][locale]
                    )
                    for finding in findings:
                        unit_failed = True
                        errors.append(
                            {
                                **finding,
                                "message": "Translation failed deterministic validation.",
                                "unit_id": unit.id,
                                "target_locale": locale,
                            }
                        )
                if not unit_failed:
                    translated[unit.id] = batch_translated[unit.id]
                    self.session.remember(unit.text, batch_translated[unit.id])

        if resolved_locale == "und":
            warnings.append(
                {
                    "code": "SOURCE_LOCALE_UNRESOLVED",
                    "message": "The provider did not return a resolved source locale.",
                }
            )
        return {
            "translated": translated,
            "resolved_source_locale": resolved_locale,
            "errors": errors,
            "warnings": warnings,
            "provider_calls": provider_calls,
            "provider_requests": provider_requests,
            "format_strategy_counts": format_strategy_counts,
            **usage_totals,
        }

    def _finalize(self, state: GraphState) -> GraphState:
        if "extraction" not in state:
            return {"result": {}}
        request = state["request"]
        extraction = state["extraction"]
        translated = state.get("translated", {})
        units = []
        for unit in extraction.units:
            if unit.id not in translated:
                continue
            units.append(
                {
                    "id": unit.id,
                    "source": {"text": unit.text, "context": unit.context},
                    "translations": {
                        locale: {"text": text, "status": "translated"}
                        for locale, text in translated[unit.id].items()
                    },
                    "protected_tokens": protected_tokens(unit.text),
                }
            )
        bundle = {
            "schema_version": "translator-agent/v1",
            "kind": "translation_bundle",
            "source_locale": state.get("resolved_source_locale", request.source.locale),
            "target_locales": request.targets,
            "units": units,
        }
        artifacts = list(state.get("artifacts", []))
        should_write = request.mode in {
            "translate_text_to_file",
            "translate_file_to_file",
        } or bool(request.output.path)
        errors = list(state.get("errors", []))
        if should_write and request.output.path and units:
            try:
                target = resolve_within(
                    self.config.output_root,
                    request.output.path,
                    must_exist=False,
                )
                single_target = request.targets[0] if len(request.targets) == 1 else None
                data, _media_type = render_output(
                    extraction, bundle, single_target, request.output.format
                )
                atomic_write(target, data, overwrite=request.output.overwrite)
                artifacts.append(artifact_record(target, self.config.output_root))
            except Exception as exc:
                errors.append(
                    {
                        "code": "OUTPUT_ERROR",
                        "message": str(exc),
                        "retryable": False,
                    }
                )
        if request.output.metrics_path:
            try:
                target = resolve_within(
                    self.config.output_root,
                    request.output.metrics_path,
                    must_exist=False,
                )
                record = {
                    "schema_version": "translator-agent/run-v1",
                    "request_id": request.request_id,
                    "mode": request.mode,
                    "status": _status(errors, len(units), len(extraction.units)),
                    "provider": {
                        "name": self.config.provider,
                        "model": self.config.model,
                    },
                    "source_locale": state.get(
                        "resolved_source_locale", request.source.locale
                    ),
                    "target_locales": request.targets,
                    "metrics": _metrics(state, len(extraction.units)),
                    "formatting": {
                        "configured_strategy": self.config.response_format_strategy,
                        "strategy_requests": state.get("format_strategy_counts", {}),
                        "max_output_tokens": self.config.max_output_tokens,
                        "reasoning_effort": self.config.reasoning_effort,
                    },
                    "warnings": state.get("warnings", []),
                    "errors": errors,
                    "translation_artifacts": artifacts,
                }
                data = json.dumps(
                    record, ensure_ascii=False, indent=2, sort_keys=True
                ).encode("utf-8") + b"\n"
                atomic_write(target, data, overwrite=request.output.overwrite)
                artifacts.append(artifact_record(target, self.config.output_root))
            except Exception as exc:
                errors.append(
                    {
                        "code": "OUTPUT_ERROR",
                        "message": f"could not write run record: {exc}",
                        "retryable": False,
                    }
                )
        return {"result": bundle, "artifacts": artifacts, "errors": errors}

    def _response(self, state: GraphState) -> dict[str, Any]:
        request = state["request"]
        errors = state.get("errors", [])
        completed = len(state.get("result", {}).get("units", []))
        total = len(state.get("extraction", Extraction([], "none")).units)
        status = _status(errors, completed, total)
        return {
            "schema_version": "translator-agent/v1",
            "request_id": request.request_id,
            "status": status,
            "mode": request.mode,
            "runtime": {
                "session_id": self.config.session_id,
                "request_sequence": state["request_sequence"],
                "context_policy": request.session.context_policy,
                "context_reused": request.session.context_policy == "continue",
            },
            "resolved_source_locale": state.get(
                "resolved_source_locale", request.source.locale
            ),
            "result": state.get("result", {}),
            "artifacts": state.get("artifacts", []),
            "warnings": state.get("warnings", []),
            "errors": errors,
            "metrics": _metrics(state, total),
        }


def _status(errors: list[dict[str, Any]], completed: int, total: int) -> str:
    if errors and completed == 0:
        return "failed"
    if errors or completed < total:
        return "partial"
    return "succeeded"


def _metrics(state: GraphState, unit_count: int) -> dict[str, Any]:
    calls = state.get("provider_calls", 0)
    reported = state.get("usage_reported_attempts", 0)
    missing = state.get("usage_missing_attempts", 0)
    return {
        "unit_count": unit_count,
        "model_calls": calls,
        "provider_requests": state.get("provider_requests", calls),
        "format_strategy_requests": state.get("format_strategy_counts", {}),
        "input_tokens": state.get("input_tokens") if reported else None,
        "output_tokens": state.get("output_tokens") if reported else None,
        "total_tokens": state.get("total_tokens") if reported else None,
        "cached_input_tokens": state.get("cached_input_tokens")
        if state.get("cached_input_reported_attempts", 0)
        else None,
        "reasoning_tokens": state.get("reasoning_tokens")
        if state.get("reasoning_reported_attempts", 0)
        else None,
        "provider_reported_cost": state.get("provider_cost")
        if state.get("provider_cost_reported_attempts", 0)
        else None,
        "usage_reported_attempts": reported,
        "usage_missing_attempts": missing,
        "usage_complete": calls > 0 and missing == 0 and reported == calls,
    }


def _accumulate_usage(
    totals: dict[str, int | float], usage: ProviderUsage
) -> None:
    complete = usage.input_tokens is not None and usage.output_tokens is not None
    if complete:
        totals["usage_reported_attempts"] += 1
    else:
        totals["usage_missing_attempts"] += 1
    for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
        value = getattr(usage, key)
        if value is not None:
            totals[key] += value
    if usage.cached_input_tokens is not None:
        totals["cached_input_reported_attempts"] += 1
    if usage.reasoning_tokens is not None:
        totals["reasoning_reported_attempts"] += 1
    total = usage.total_tokens
    if total is None and complete:
        total = usage.input_tokens + usage.output_tokens
    if total is not None:
        totals["total_tokens"] += total
    if usage.provider_cost is not None:
        totals["provider_cost"] += usage.provider_cost
        totals["provider_cost_reported_attempts"] += 1


def _batch_units(units: list[ExtractedUnit], max_chars: int) -> list[list[ExtractedUnit]]:
    batches: list[list[ExtractedUnit]] = []
    current: list[ExtractedUnit] = []
    current_size = 0
    for unit in units:
        size = len(unit.text) + len(unit.id) + 32
        if current and current_size + size > max_chars:
            batches.append(current)
            current, current_size = [], 0
        current.append(unit)
        current_size += size
    if current:
        batches.append(current)
    return batches


def _normalize_translations(
    payload: dict[str, Any], units: list[ExtractedUnit], targets: list[str]
) -> dict[str, dict[str, str]]:
    raw = payload.get("translations")
    if not isinstance(raw, list):
        raise ValueError("model output is missing translations array")
    by_id: dict[str, dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("targets"), dict):
            continue
        normalized_targets: dict[str, str] = {}
        for locale, value in item["targets"].items():
            if isinstance(value, str):
                normalized_targets[str(locale)] = value
            elif isinstance(value, dict) and isinstance(value.get("text"), str):
                normalized_targets[str(locale)] = value["text"]
        by_id[str(item.get("id"))] = normalized_targets
    expected = {unit.id for unit in units}
    # A low-cost model may translate the sole requested unit correctly while
    # copying or inventing its ID. With exactly one input and one output there
    # is no ambiguity, so normalize the identifier instead of spending another
    # model call on a purely structural repair.
    if len(expected) == 1 and len(by_id) == 1 and set(by_id) != expected:
        by_id = {next(iter(expected)): next(iter(by_id.values()))}
    if set(by_id) != expected:
        raise ValueError("model output unit IDs do not match the request")
    for unit_id, values in by_id.items():
        if any(locale not in values for locale in targets):
            raise ValueError(f"model output targets are incomplete for {unit_id}")
    return {unit_id: {locale: values[locale] for locale in targets} for unit_id, values in by_id.items()}


def _add_repair_instruction(
    messages: list[dict[str, str]],
    repair: Any,
) -> list[dict[str, str]]:
    retry_messages = [dict(message) for message in messages]
    payload = json.loads(retry_messages[1]["content"])
    payload["repair"] = repair
    retry_messages[1]["content"] = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return retry_messages


def _model_output_errors(
    units: list[ExtractedUnit],
    targets: list[str],
    exc: ProviderError | ValueError,
) -> list[dict[str, Any]]:
    code = "PROVIDER_ERROR" if isinstance(exc, ProviderError) else "INVALID_MODEL_OUTPUT"
    return _batch_errors(units, targets, code, str(exc), True)


def _batch_errors(
    units: list[ExtractedUnit],
    targets: list[str],
    code: str,
    message: str,
    retryable: bool,
) -> list[dict[str, Any]]:
    return [
        {
            "code": code,
            "message": message,
            "unit_id": unit.id,
            "target_locale": locale,
            "retryable": retryable,
        }
        for unit in units
        for locale in targets
    ]
