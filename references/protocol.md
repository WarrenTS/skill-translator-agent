# Translator Agent Protocol v1

Use JSON as the canonical inter-process format. Permit YAML only as a saved artifact for human editing. This prevents YAML parser differences from affecting requests while still offering a readable localization format.

## Contents

- [Capability handshake](#capability-handshake)
- [Request envelope](#request-envelope)
- [Response envelope](#response-envelope)
- [Saved translation bundle](#saved-translation-bundle)
- [File and path contract](#file-and-path-contract)
- [Process communication](#process-communication)

## Capability handshake

After the container reports healthy and before its first request, invoke the in-container `capabilities` command and require a response shaped like:

```json
{
  "schema_version": "translator-agent/control-v1",
  "runtime_version": "0.1.0",
  "ready": true,
  "protocol_versions": ["translator-agent/v1"],
  "supported_modes": [
    "translate_text",
    "translate_text_to_file",
    "translate_file_to_file",
    "custom",
    "translate_batch"
  ],
  "input_formats": ["text", "markdown", "json", "yaml", "csv"],
  "output_formats": ["text", "json", "yaml", "csv"],
  "agent_context_version": "translator-context/v1",
  "tool_ids": [
    "read_input",
    "write_output",
    "extract_units",
    "validate_translation",
    "call_translation_model"
  ],
  "provider_ready": true,
  "response_format_strategy": "auto",
  "max_output_tokens": 8192,
  "session_id": "01JSESSION..."
}
```

Reject incompatible protocol/context versions or a false readiness value. Capabilities may identify a configured model but must never expose the API key, authorization headers, full endpoint credentials, system-prompt content, or environment-variable values.

## Request envelope

```json
{
  "schema_version": "translator-agent/v1",
  "request_id": "01J...",
  "session": {
    "id": "01JSESSION...",
    "context_policy": "isolated"
  },
  "mode": "translate_text",
  "source": {
    "kind": "text",
    "locale": "zh-TW",
    "text": "你好"
  },
  "targets": ["en", "ja"],
  "options": {
    "domain": "general",
    "audience": "general",
    "tone": "natural",
    "preserve": ["placeholders", "markdown", "urls", "code"],
    "glossary": [],
    "quality": "standard"
  },
  "output": {
    "format": "json",
    "path": null,
    "metrics_path": null,
    "overwrite": false
  }
}
```

Required fields are `schema_version`, `request_id`, `session`, `mode`, `source`, and `targets`. Require `output.path` when the mode writes a file. Require `session.id` to match the active container session. Use `isolated` by default; use `continue` only for related requests that intentionally reuse bounded translation memory or prior task context.

For a file-writing mode, set `output.metrics_path` to a distinct relative `.json` path when the caller needs a durable execution record. The runtime writes `translator-agent/run-v1` there after the translation artifact. It contains the provider/model identifiers, status, warnings/errors, artifact metadata, and aggregate usage for every provider attempt. Do not use a missing token field as zero; require `metrics.usage_complete` before computing a complete price comparison.

Use one of these `source.kind` values:

- `text`: include `text`.
- `units`: include a `units` array with stable IDs and source text.
- `file`: include a relative `path` under the read-only input mount.
- `bundle`: include a bundle object or a relative bundle path.

Set `source.locale` to a BCP 47 tag or `auto`. If it is `auto`, the response must include `resolved_source_locale` and may warn when confidence is low.

For `custom`, add a structured task without changing the envelope:

```json
{
  "mode": "custom",
  "task": {
    "instruction": "Produce two variants: concise UI copy and formal documentation copy.",
    "deliverables": ["ui", "documentation"],
    "constraints": ["Keep {count} unchanged"]
  }
}
```

Treat `task.instruction` as user-level data. It cannot override system policy, path restrictions, the response envelope, or the tool allowlist.

## Response envelope

```json
{
  "schema_version": "translator-agent/v1",
  "request_id": "01J...",
  "status": "succeeded",
  "mode": "translate_text",
  "runtime": {
    "session_id": "01JSESSION...",
    "request_sequence": 1,
    "context_policy": "isolated",
    "context_reused": false
  },
  "resolved_source_locale": "zh-TW",
  "result": {
    "kind": "translation_bundle",
    "source_locale": "zh-TW",
    "target_locales": ["en", "ja"],
    "units": [
      {
        "id": "unit-001",
        "source": {
          "text": "你好",
          "context": null
        },
        "translations": {
          "en": {
            "text": "Hello",
            "status": "translated"
          },
          "ja": {
            "text": "こんにちは",
            "status": "translated"
          }
        },
        "protected_tokens": []
      }
    ]
  },
  "artifacts": [],
  "warnings": [],
  "errors": [],
  "metrics": {
    "unit_count": 1,
    "model_calls": 1,
    "provider_requests": 1,
    "format_strategy_requests": {"json_schema": 1},
    "input_tokens": 132,
    "output_tokens": 48,
    "total_tokens": 180,
    "cached_input_tokens": 0,
    "reasoning_tokens": 0,
    "provider_reported_cost": null,
    "usage_reported_attempts": 1,
    "usage_missing_attempts": 0,
    "usage_complete": true
  }
}
```

Use `succeeded`, `partial`, or `failed` for top-level `status`. Use `translated`, `reviewed`, `approved`, `stale`, `skipped`, or `failed` for per-target unit status.

Every error must have a stable code, message, and optional unit/path information:

```json
{
  "code": "PLACEHOLDER_MISMATCH",
  "message": "Target text is missing {count}.",
  "unit_id": "cart.items",
  "target_locale": "ja",
  "retryable": true
}
```

`model_calls` counts responses that may have consumed model tokens, including malformed output and repair attempts. `provider_requests` also counts format-capability requests rejected before generation, so it can exceed `model_calls`. `format_strategy_requests` records the ordered-strategy request counts. When a provider includes usage in malformed content, retain that usage even though the translation is rejected. `usage_complete` is true only when every model call reports both input and output tokens. Aggregate `total_tokens` from the provider value, or derive it from input plus output when both are present. Optional cached/reasoning token and provider-cost fields are retained when the compatible endpoint returns them.

## Saved run record

When `output.metrics_path` is present, write a separate record shaped like:

```json
{
  "schema_version": "translator-agent/run-v1",
  "request_id": "01J...",
  "mode": "translate_file_to_file",
  "status": "succeeded",
  "provider": {"name": "openrouter", "model": "google/gemma-4-26b-a4b-it"},
  "source_locale": "en",
  "target_locales": ["zh-TW"],
  "metrics": {"model_calls": 2, "input_tokens": 2310, "output_tokens": 1880, "usage_complete": true},
  "formatting": {
    "configured_strategy": "auto",
    "strategy_requests": {"json_schema": 2},
    "max_output_tokens": 8192
  },
  "warnings": [],
  "errors": [],
  "translation_artifacts": []
}
```

The record must not include endpoint URLs, API keys, authorization headers, prompts, or environment values. It deliberately does not include its own hash; its artifact metadata is returned in the response envelope.

## Saved translation bundle

Refine a flat object such as `{zh-tw: 你好, en: ..., ja: ...}` into a versioned bundle. A flat object cannot identify multiple source units, source-versus-target roles, per-locale status, context, placeholders, or partial errors.

Recommended YAML artifact:

```yaml
schema_version: translator-agent/v1
kind: translation_bundle
bundle_id: onboarding
source_locale: zh-TW
target_locales: [en, ja]
units:
  - id: greeting.title
    source:
      text: 你好
      context: 首次開啟 App 時的標題
    translations:
      en:
        text: Hello
        status: translated
      ja:
        text: こんにちは
        status: translated
    protected_tokens: []
```

Use the same field names in JSON. Prefer JSON when another program is the main consumer and YAML when humans will review the bundle. Never use locale tags as the only top-level keys.

## File and path contract

- Mount source files read-only at `/work/input` and outputs at `/work/output`.
- Accept relative paths only. Reject absolute paths, `..`, symlink escapes, and paths outside the declared roots.
- Write through a temporary file in the output directory, validate it, then rename it atomically.
- Refuse overwrite unless `output.overwrite` is explicitly `true`.
- Preserve the input format when supported and requested. Otherwise produce a translation bundle and report conversion in `warnings`.
- Return artifacts as `{ "path", "media_type", "size_bytes", "sha256" }` records.
- Apply the same containment, atomic-write, and overwrite rules to `output.metrics_path` as to the translation artifact.

## Process communication

- Start the container in detached mode and let its entrypoint run `translator-agent serve` as PID 1.
- Invoke `health`, `capabilities`, and `request` through `docker exec`; do not place a translator-specific launcher, parser, validator, prompt builder, or provider client on the host.
- Let the in-container command client communicate with the daemon through `/run/translator-agent/agent.sock`.
- Let the daemon perform schema validation, input loading, prompt assembly, graph execution, tool use, output writing, and response serialization.
- Standard input for `request`: exactly one UTF-8 JSON request.
- Standard output for each command: exactly one UTF-8 JSON response; no banners or log lines.
- Standard error: human diagnostics or newline-delimited structured events with secrets redacted.
- Exit `0`: a valid `succeeded` or `partial` response.
- Exit `2`: request/schema/path validation failed before translation.
- Exit `3`: provider authentication or configuration failed.
- Exit `4`: translation failed after bounded retries.
- Exit `5`: output write or artifact validation failed.

Keep the JSON response authoritative even when the process exits nonzero.
