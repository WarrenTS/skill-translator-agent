from __future__ import annotations

import re


TOKEN_PATTERN = re.compile(
    r"\{\{[^{}]+\}\}|\{[A-Za-z_][^{}]*\}|%(?:\d+\$)?[sdif]|https?://[^\s)\]}>]+"
)


def protected_tokens(text: str) -> list[str]:
    return sorted(set(TOKEN_PATTERN.findall(text)))


def validate_target(source: str, target: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    if source and not target.strip():
        findings.append({"code": "EMPTY_TRANSLATION", "retryable": True})
    source_tokens = set(protected_tokens(source))
    target_tokens = set(protected_tokens(target))
    missing = sorted(source_tokens - target_tokens)
    extra = sorted(target_tokens - source_tokens)
    if missing or extra:
        findings.append(
            {
                "code": "PROTECTED_TOKEN_MISMATCH",
                "missing": missing,
                "extra": extra,
                "retryable": True,
            }
        )
    return findings
