from __future__ import annotations

import json
from typing import Any

from translator_agent.models import ExtractedUnit, TranslationRequest


def build_messages(
    system_prompt: str,
    request: TranslationRequest,
    units: list[ExtractedUnit],
    memory: list[dict[str, Any]],
) -> list[dict[str, str]]:
    options: dict[str, Any] = {
        "domain": request.options.domain,
        "audience": request.options.audience,
        "tone": request.options.tone,
        "preserve": request.options.preserve,
    }
    if request.options.glossary:
        options["glossary"] = [item.model_dump() for item in request.options.glossary]
    task = request.task.model_dump(exclude_none=True) if request.task else None
    payload = {
        "source_locale": request.source.locale,
        "target_locales": request.targets,
        "options": options,
        "task": task,
        "memory": memory,
        "units": [unit.model_dump(exclude_none=True) for unit in units],
        "return": {
            "detected_source_locale": "BCP-47-or-und",
            "translations": [{"id": "unit-id", "targets": {"locale": "text"}}],
        },
    }
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ),
        },
    ]


def translation_response_schema(targets: list[str]) -> dict[str, Any]:
    target_properties = {locale: {"type": "string"} for locale in targets}
    return {
        "type": "object",
        "properties": {
            "detected_source_locale": {"type": "string"},
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "targets": {
                            "type": "object",
                            "properties": target_properties,
                            "required": targets,
                            "additionalProperties": False,
                        },
                    },
                    "required": ["id", "targets"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["detected_source_locale", "translations"],
        "additionalProperties": False,
    }
