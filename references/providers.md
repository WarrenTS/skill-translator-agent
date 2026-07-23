# Provider Presets

Use these presets only after the user selects a provider. Always ask for the exact model ID and let the user enter secrets locally in `.transenv`.

| Provider | `TRANSLATOR_PROVIDER` | Base URL | Default auth |
| --- | --- | --- | --- |
| OpenAI API | `openai` | `https://api.openai.com/v1` | `required` |
| NVIDIA hosted NIM | `nvidia` | `https://integrate.api.nvidia.com/v1` | `required` |
| OpenRouter | `openrouter` | `https://openrouter.ai/api/v1` | `required` |
| Custom/self-hosted | `custom` | User-provided | Ask: `required`, `optional`, or `none` |

Official references:

- [OpenAI API reference](https://platform.openai.com/docs/api-reference)
- [NVIDIA hosted endpoint documentation](https://docs.nvidia.com/nim-operator/latest/guardrail.html)
- [OpenRouter quickstart](https://openrouter.ai/docs/quickstart)

## Authentication behavior

- `required`: Reject startup readiness when `TRANSLATOR_API_KEY` is blank or still contains a template placeholder. Send a Bearer header for requests.
- `optional`: Accept a blank key and send no Authorization header. Send a Bearer header only when the user supplied a key.
- `none`: Accept a blank key and never send an Authorization header, even if an unrelated key exists in the environment.

For a self-hosted endpoint without authentication, use `custom` plus `none`. For an endpoint that may enable authentication later, use `custom` plus `optional`.

Require a base URL, not a full `/chat/completions` URL. Normalize one trailing slash and append `/chat/completions`. Preserve provider-specific `/v1` or `/api/v1` prefixes.

Allow plain HTTP only for a user-confirmed self-hosted endpoint with `TRANSLATOR_ALLOW_INSECURE_HTTP=true`. Never enable it automatically for public endpoints.

## Structured-output compatibility

Set `TRANSLATOR_RESPONSE_FORMAT=auto` by default. The runtime attempts `json_schema`, then `json_object`, then prompt-only JSON only when the endpoint explicitly rejects or fails to produce the stronger format. Use an explicit value (`json_schema`, `json_object`, or `prompt`) only for a known endpoint capability or a controlled compatibility test.

Set `TRANSLATOR_MAX_OUTPUT_TOKENS` as a hard ceiling, not a fixed charge. The runtime derives a smaller per-call completion budget from source characters and target count. OpenRouter requests exclude reasoning text from the returned message and default `TRANSLATOR_REASONING_EFFORT=none` because translation does not benefit from hidden deliberation. If an endpoint explicitly reports that reasoning is mandatory, the runtime retries the same request once with `minimal`; configure a higher effort only when deliberately required. Any reasoning usage remains chargeable and is retained in metrics when reported.
