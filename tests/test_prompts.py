from translator_agent.prompts import translation_response_schema


def test_translation_schema_requires_exact_target_locales():
    schema = translation_response_schema(["zh-TW", "ja"])
    targets = schema["properties"]["translations"]["items"]["properties"]["targets"]

    assert targets["required"] == ["zh-TW", "ja"]
    assert set(targets["properties"]) == {"zh-TW", "ja"}
    assert targets["additionalProperties"] is False
    assert schema["additionalProperties"] is False
