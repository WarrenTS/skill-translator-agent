from __future__ import annotations

import copy
import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from translator_agent.config import RuntimeConfig
from translator_agent.models import ExtractedUnit, TranslationRequest
from translator_agent.security import resolve_within
from translator_agent.timeouts import effective_batch_chars


@dataclass
class Extraction:
    units: list[ExtractedUnit]
    source_format: str
    original: Any = None
    input_path: Path | None = None


def extract_source(request: TranslationRequest, config: RuntimeConfig) -> Extraction:
    source = request.source
    if source.kind == "text":
        chunks = _split_text(
            source.text or "",
            _text_unit_limit(config.max_batch_chars, len(request.targets)),
        )
        return Extraction(
            units=_text_units(chunks, "unit"),
            source_format="text",
        )
    if source.kind == "units":
        return Extraction(
            units=[ExtractedUnit(**unit.model_dump()) for unit in source.units or []],
            source_format="units",
        )
    if source.kind == "bundle" and source.bundle is not None:
        return _extract_bundle(source.bundle)
    if not source.path:
        raise ValueError("file or bundle source requires path")
    path = resolve_within(config.input_root, source.path, must_exist=True)
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in {".txt", ".md"}:
        chunks = _split_text(
            text,
            _text_unit_limit(config.max_batch_chars, len(request.targets)),
        )
        return Extraction(
            units=_text_units(chunks, path.name),
            source_format="markdown" if suffix == ".md" else "text",
            original=text,
            input_path=path,
        )
    if suffix == ".json":
        original = json.loads(text)
        return _extract_tree(original, "json", path)
    if suffix in {".yaml", ".yml"}:
        original = yaml.safe_load(text)
        return _extract_tree(original, "yaml", path)
    if suffix == ".csv":
        rows = list(csv.reader(io.StringIO(text)))
        units = [
            ExtractedUnit(id=f"row-{row_index}.col-{column_index}", text=cell)
            for row_index, row in enumerate(rows)
            for column_index, cell in enumerate(row)
            if cell
        ]
        return Extraction(units=units, source_format="csv", original=rows, input_path=path)
    raise ValueError(f"unsupported input format: {suffix or '<none>'}")


def _extract_bundle(bundle: dict[str, Any]) -> Extraction:
    units: list[ExtractedUnit] = []
    for item in bundle.get("units", []):
        source = item.get("source", {}) if isinstance(item, dict) else {}
        units.append(
            ExtractedUnit(
                id=str(item.get("id")),
                text=str(source.get("text", "")),
                context=source.get("context"),
            )
        )
    return Extraction(units=units, source_format="bundle", original=bundle)


def _extract_tree(original: Any, source_format: str, path: Path) -> Extraction:
    units: list[ExtractedUnit] = []

    def walk(value: Any, parts: list[str]) -> None:
        if isinstance(value, str):
            units.append(ExtractedUnit(id="/" + "/".join(parts), text=value))
        elif isinstance(value, dict):
            for key, child in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                walk(child, [*parts, escaped])
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, [*parts, str(index)])

    walk(original, [])
    return Extraction(
        units=units, source_format=source_format, original=original, input_path=path
    )


def render_output(
    extraction: Extraction,
    bundle: dict[str, Any],
    target_locale: str | None,
    output_format: str,
) -> tuple[bytes, str]:
    if output_format == "source":
        output_format = extraction.source_format
    if output_format in {"json", "yaml"} and (
        target_locale is None
        or extraction.source_format not in {"json", "yaml"}
        or extraction.source_format in {"units", "bundle"}
    ):
        if output_format == "json":
            return (
                json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json",
            )
        return (
            yaml.safe_dump(bundle, allow_unicode=True, sort_keys=False).encode("utf-8"),
            "application/yaml",
        )

    if target_locale is None:
        raise ValueError("preserving source structure requires exactly one target locale")
    translated = {
        unit["id"]: unit["translations"][target_locale]["text"]
        for unit in bundle["units"]
    }
    if extraction.source_format in {"text", "markdown"}:
        value = "".join(translated.get(unit.id, "") for unit in extraction.units)
        return value.encode("utf-8"), "text/markdown" if extraction.source_format == "markdown" else "text/plain"
    if extraction.source_format in {"json", "yaml"}:
        rebuilt = copy.deepcopy(extraction.original)
        for pointer, value in translated.items():
            _set_pointer(rebuilt, pointer, value)
        if output_format == "json" or extraction.source_format == "json":
            return json.dumps(rebuilt, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"
        return yaml.safe_dump(rebuilt, allow_unicode=True, sort_keys=False).encode("utf-8"), "application/yaml"
    if extraction.source_format == "csv":
        rows = copy.deepcopy(extraction.original)
        for unit_id, value in translated.items():
            row_part, column_part = unit_id.split(".")
            row = int(row_part.removeprefix("row-"))
            column = int(column_part.removeprefix("col-"))
            rows[row][column] = value
        stream = io.StringIO(newline="")
        csv.writer(stream).writerows(rows)
        return stream.getvalue().encode("utf-8"), "text/csv"
    if output_format == "text" and len(translated) == 1:
        return next(iter(translated.values())).encode("utf-8"), "text/plain"
    raise ValueError(f"cannot render {extraction.source_format} as {output_format}")


def _set_pointer(root: Any, pointer: str, value: str) -> None:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    cursor = root
    for part in parts[:-1]:
        cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
    final = parts[-1]
    if isinstance(cursor, list):
        cursor[int(final)] = value
    else:
        cursor[final] = value


def _text_unit_limit(max_batch_chars: int, target_count: int) -> int:
    return effective_batch_chars(max_batch_chars, target_count)


def _text_units(chunks: list[str], base_id: str) -> list[ExtractedUnit]:
    if len(chunks) == 1:
        unit_id = "unit-001" if base_id == "unit" else base_id
        return [ExtractedUnit(id=unit_id, text=chunks[0])]
    return [
        ExtractedUnit(id=f"{base_id}#chunk-{index:04d}", text=chunk)
        for index, chunk in enumerate(chunks, start=1)
    ]


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split contiguous text at line boundaries, retaining every source character."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_chars])
            line = line[max_chars:]
        if current and len(current) + len(line) > max_chars:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks or [text]
