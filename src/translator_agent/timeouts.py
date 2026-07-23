from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from translator_agent.config import RuntimeConfig
from translator_agent.security import PathSecurityError, resolve_within


TIMEOUT_POLICY_ID = "adaptive-v1"
_FIXED_PROVIDER_OVERHEAD_SECONDS = 15
_ESTIMATED_WEIGHTED_CHARS_PER_SECOND = 150
_MAX_PROVIDER_TIMEOUT_SECONDS = 600
_MAX_CONTROL_TIMEOUT_SECONDS = 1800
_CONTROL_BUFFER_SECONDS = 5


def provider_timeout_seconds(
    base_seconds: int,
    source_chars: int,
    target_count: int,
) -> int:
    """Estimate one provider pass from source size and translation fan-out."""
    weighted_chars = max(1, source_chars) * max(1, target_count)
    estimated = _FIXED_PROVIDER_OVERHEAD_SECONDS + math.ceil(
        weighted_chars / _ESTIMATED_WEIGHTED_CHARS_PER_SECOND
    )
    return min(_MAX_PROVIDER_TIMEOUT_SECONDS, max(base_seconds, estimated))


def effective_batch_chars(max_batch_chars: int, target_count: int) -> int:
    """Bound source per call for multilingual JSON output, not input alone."""
    output_expansion_factor = max(1, target_count) * 2
    return max(256, max_batch_chars // output_expansion_factor)


def completion_token_budget(
    source_chars: int,
    target_count: int,
    hard_max_tokens: int,
) -> int:
    """Budget translated text plus JSON framing without always paying the hard cap."""
    weighted_chars = max(1, source_chars) * max(1, target_count)
    estimated = (weighted_chars * 11 + 9) // 10 + 512
    return min(hard_max_tokens, max(512, estimated))


def control_timeout_seconds(config: RuntimeConfig, payload: Any) -> int:
    """Estimate the full bounded job, including an optional repair pass per batch."""
    if not isinstance(payload, dict):
        return 10
    source_chars = _estimate_source_chars(payload, config.input_root)
    target_count = _target_count(payload)
    batch_limit = effective_batch_chars(config.max_batch_chars, target_count)
    batch_sizes = _estimated_batch_sizes(source_chars, batch_limit)
    provider_passes = 1 + config.max_retries
    format_attempts = 3 if config.response_format_strategy == "auto" else 1
    total = sum(
        provider_timeout_seconds(config.timeout_seconds, size, target_count)
        * provider_passes
        * format_attempts
        for size in batch_sizes
    )
    return min(
        _MAX_CONTROL_TIMEOUT_SECONDS,
        max(10, total + _CONTROL_BUFFER_SECONDS),
    )


def _target_count(payload: dict[str, Any]) -> int:
    targets = payload.get("targets")
    return len(targets) if isinstance(targets, list) and targets else 1


def _estimated_batch_sizes(source_chars: int, max_batch_chars: int) -> list[int]:
    remaining = max(1, source_chars)
    sizes: list[int] = []
    while remaining:
        size = min(remaining, max_batch_chars)
        sizes.append(size)
        remaining -= size
    return sizes


def _estimate_source_chars(payload: dict[str, Any], input_root: Path) -> int:
    source = payload.get("source")
    if not isinstance(source, dict):
        return 1
    kind = source.get("kind")
    if kind == "text":
        return len(str(source.get("text") or ""))
    if kind == "units":
        units = source.get("units")
        if isinstance(units, list):
            return sum(
                len(str(item.get("text") or ""))
                for item in units
                if isinstance(item, dict)
            )
    if kind in {"file", "bundle"} and isinstance(source.get("path"), str):
        try:
            path = resolve_within(input_root, source["path"], must_exist=True)
            return path.stat().st_size
        except (OSError, PathSecurityError):
            return 1
    if kind == "bundle" and isinstance(source.get("bundle"), dict):
        return len(json.dumps(source["bundle"], ensure_ascii=False))
    return 1
