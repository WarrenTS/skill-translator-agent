import json

from translator_agent.adapters import extract_source, render_output
from translator_agent.graph import _normalize_translations
from translator_agent.models import ExtractedUnit
from translator_agent.models import TranslationRequest


def test_json_file_preserves_keys(mock_provider, runtime_factory):
    base_url, _handler = mock_provider
    config, _assets = runtime_factory(base_url)
    (config.input_root / "source.json").write_text(
        json.dumps({"title": "你好", "count": 3}), encoding="utf-8"
    )
    request = TranslationRequest.model_validate(
        {
            "schema_version": "translator-agent/v1",
            "request_id": "request-1",
            "session": {"id": "test-session", "context_policy": "isolated"},
            "mode": "translate_file_to_file",
            "source": {"kind": "file", "locale": "zh-TW", "path": "source.json"},
            "targets": ["en"],
            "output": {"format": "source", "path": "target.json"},
        }
    )
    extraction = extract_source(request, config)
    assert [unit.id for unit in extraction.units] == ["/title"]
    bundle = {
        "units": [
            {
                "id": "/title",
                "translations": {"en": {"text": "Hello"}},
            }
        ]
    }
    data, media_type = render_output(extraction, bundle, "en", "source")
    assert json.loads(data) == {"title": "Hello", "count": 3}
    assert media_type == "application/json"


def test_text_file_splits_at_bounded_units_without_losing_source(runtime_factory):
    config, _assets = runtime_factory("http://127.0.0.1:1/v1")
    source_text = "".join(f"Line {index}\n" for index in range(700))
    (config.input_root / "source.txt").write_text(source_text, encoding="utf-8")
    request = TranslationRequest.model_validate(
        {
            "schema_version": "translator-agent/v1",
            "request_id": "request-1",
            "session": {"id": "test-session", "context_policy": "isolated"},
            "mode": "translate_file_to_file",
            "source": {"kind": "file", "locale": "en", "path": "source.txt"},
            "targets": ["zh-TW", "zh-HK", "ja"],
            "output": {"format": "json", "path": "target.json"},
        }
    )

    extraction = extract_source(request, config)

    assert len(extraction.units) > 1
    assert "".join(unit.text for unit in extraction.units) == source_text
    assert extraction.units[0].id == "source.txt#chunk-0001"


def test_text_render_joins_translated_chunks_in_source_order(runtime_factory):
    config, _assets = runtime_factory("http://127.0.0.1:1/v1")
    source_text = "x" * 5000
    (config.input_root / "source.txt").write_text(source_text, encoding="utf-8")
    request = TranslationRequest.model_validate(
        {
            "schema_version": "translator-agent/v1",
            "request_id": "request-1",
            "session": {"id": "test-session", "context_policy": "isolated"},
            "mode": "translate_file_to_file",
            "source": {"kind": "file", "locale": "en", "path": "source.txt"},
            "targets": ["ja", "zh-TW", "zh-HK"],
            "output": {"format": "source", "path": "target.txt"},
        }
    )
    extraction = extract_source(request, config)
    bundle = {
        "units": [
            {
                "id": unit.id,
                "translations": {"ja": {"text": f"part-{index}\n"}},
            }
            for index, unit in enumerate(extraction.units, start=1)
        ]
    }

    data, media_type = render_output(extraction, bundle, "ja", "source")

    assert data.decode() == "".join(
        f"part-{index}\n" for index in range(1, len(extraction.units) + 1)
    )
    assert media_type == "text/plain"


def test_model_output_accepts_text_wrapper_without_stringifying_dict():
    units = [ExtractedUnit(id="unit-001", text="hello")]
    payload = {
        "translations": [
            {
                "id": "unit-001",
                "targets": {"zh-TW": {"text": "你好"}, "ja": "こんにちは"},
            }
        ]
    }

    normalized = _normalize_translations(payload, units, ["zh-TW", "ja"])

    assert normalized == {
        "unit-001": {"zh-TW": "你好", "ja": "こんにちは"}
    }


def test_single_unit_output_normalizes_an_unambiguous_wrong_id():
    units = [ExtractedUnit(id="requested-id", text="hello")]
    payload = {
        "translations": [
            {"id": "invented-id", "targets": {"zh-TW": "你好"}}
        ]
    }

    normalized = _normalize_translations(payload, units, ["zh-TW"])

    assert normalized == {"requested-id": {"zh-TW": "你好"}}
