# Runtime Architecture and Implementation Plan

## Contents

- [Boundaries](#boundaries)
- [Container lifecycle](#container-lifecycle)
- [Request interaction](#request-interaction)
- [Official image assessment](#official-image-assessment)
- [Proposed repository layout](#proposed-repository-layout)
- [LangGraph state](#langgraph-state)
- [Graph flow](#graph-flow)
- [Context and prompts](#context-and-prompts)
- [Tool policy](#tool-policy)
- [Translation behavior](#translation-behavior)
- [Provider configuration](#provider-configuration)
- [Token and cost policy](#token-and-cost-policy)
- [Implementation phases](#implementation-phases)
- [Acceptance criteria](#acceptance-criteria)

## Boundaries

Package the complete translator runtime as a Docker image. Keep the Codex skill as the orchestrator-facing layer, but let it do only four host-side operations: construct a JSON request, choose explicit volume mounts, pass provider settings as container environment variables, and invoke Docker. Do not install or execute translator Python packages, parsers, validators, LangGraph code, or model clients on the host.

Use three trust zones:

1. The calling agent interprets the user's request and creates a protocol request.
2. The container validates the request and paths, loads content, executes LangGraph, calls the provider, validates translations, and writes artifacts inside mounted roots.
3. The OpenAI-compatible provider receives only the minimum translation context required for each unit.

The host may confirm a declared output artifact exists after the container exits, but it must not inspect or transform content as part of the translator workflow. Do not expose the Docker socket, host network namespace, home directory, repository root, or arbitrary environment variables to the container.

## Container lifecycle

Start the translator runtime before submitting a request. Run its daemon as container PID 1 and expose its control interface only through a Unix-domain socket inside the container. Do not publish an HTTP port for the default profile.

Choose one lifecycle at launch time:

- `ephemeral`: Start a dedicated detached container, wait until it is ready, submit one isolated request, capture the response, then stop and remove the container. Make cleanup run after both success and failure.
- `session`: Start or intentionally reuse a dedicated detached container for related requests. Keep its model client, bounded translation memory, and LangGraph checkpointer available until the workflow finishes or the idle timeout expires.

Assign a unique, non-user-content-derived container name and labels for skill name, runtime version, and session ID. Reuse a session container only when its provider profile, mounts, protocol version, context version, user-visible workflow, and security scope match the new request. Docker mounts are fixed at container creation; start a new container when the input/output scope changes.

Default every request to isolated graph context. Continue prior context only when `session.context_policy` is `continue` and the request is part of the same workflow. Do not carry raw source documents into unrelated requests. Stop and remove a retained container when it becomes unhealthy, configuration changes, the workflow ends, or cleanup is requested.

Configure a bounded idle timeout for retained sessions and make the daemon exit cleanly when the timeout expires. Do not keep an unbounded container merely to avoid startup cost.

## Request interaction

Use an in-container command interface so the host needs only Docker:

1. Start the image in detached mode; the image entrypoint runs `translator-agent serve`.
2. Poll `docker exec <container> translator-agent health` until ready or a bounded startup timeout expires.
3. Run `docker exec <container> translator-agent capabilities` and validate its JSON response before the first job.
4. Pipe one canonical JSON request to `docker exec -i <container> translator-agent request`.
5. The in-container client sends the request to the daemon through `/run/translator-agent/agent.sock` and prints exactly one JSON response.
6. Apply the selected lifecycle policy after capturing the response.

The `capabilities` response must include runtime and protocol versions, supported modes, input/output formats, agent-context version, registered tool IDs, provider readiness, and session ID. It must not expose prompt text, secrets, authorization headers, or provider credentials.

Keep the daemon responsible for schema validation, prompt construction, graph state, tool execution, file operations, provider calls, and response serialization. `docker exec` is transport only; no translator-specific code executes on the host.

## Official image assessment

LangChain publishes the official `langchain/langgraph-api` image, and the LangGraph CLI uses it as the default base for building a Python Agent Server. This image is an Agent Server runtime rather than a neutral image containing only the open-source `langgraph` library.

Use two deliberately separate deployment profiles:

- **Default runtime:** Base the MVP on a minimal official Python image, pin the image by digest, and install a lockfile-pinned release of the official `langgraph` Python package inside it. Run an internal daemon that supports either one-request ephemeral containers or bounded multi-request sessions. This keeps every translator component in Docker without requiring the Agent Server platform.
- **Optional Agent Server:** Base an HTTP/thread/streaming service on `langchain/langgraph-api:<pinned-tag>` or on the Dockerfile generated by `langgraph build`. Use this only when persistent runs, LangGraph Agent Server APIs, queues, or horizontal service operation are required. Plan for its documented LangSmith key/license, PostgreSQL, Redis, and deployment requirements.

Do not use a mutable `latest` or Python-major-only tag for releases. Resolve and record an immutable digest during implementation, keep the LangGraph package/server version compatible with that image, and refresh it through tested dependency updates.

Official references:

- [LangGraph CLI](https://docs.langchain.com/langsmith/cli)
- [LangGraph Agent Server](https://docs.langchain.com/langsmith/agent-server)
- [Standalone Agent Server deployment](https://docs.langchain.com/langsmith/deploy-standalone-server)
- [Official Docker Hub image](https://hub.docker.com/r/langchain/langgraph-api)

## Proposed repository layout

```text
translator-agent/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── assets/
│   └── transenv.template
├── references/
│   ├── architecture.md
│   └── protocol.md
├── Dockerfile
├── Dockerfile.agent-server
├── .dockerignore
├── compose.yaml
├── langgraph.json
├── pyproject.toml
├── config/
│   ├── agent-context.yaml
│   └── tools.yaml
├── src/translator_agent/
│   ├── cli.py
│   ├── config.py
│   ├── graph.py
│   ├── server.py
│   ├── session.py
│   ├── models.py
│   ├── prompts.py
│   ├── provider.py
│   ├── security.py
│   ├── validators.py
│   └── adapters/
│       ├── text.py
│       ├── markdown.py
│       ├── structured.py
│       └── csv.py
└── tests/
    ├── fixtures/
    ├── test_graph.py
    ├── test_protocol.py
    ├── test_security.py
    └── test_adapters.py
```

## LangGraph state

Use a typed state rather than free-form message history. Include:

- normalized request and configuration
- session ID, request sequence, context policy, and context version
- resolved source locale
- extracted units and format metadata
- glossary, style, audience, and protected tokens
- translations by unit and target locale
- validation findings and retry counters
- artifacts, warnings, errors, and metrics

Keep raw API credentials out of graph state, checkpoints, traces, and prompts. Inject a configured provider client as a runtime dependency. Namespace checkpoints and translation memory by session ID. Reset request-local state for `isolated`; load only bounded, explicitly reusable terminology and translation memory for `continue`.

## Graph flow

```text
validate_request
  -> load_source
  -> extract_units
  -> prepare_context
  -> translate_units
  -> deterministic_validate
     -> assemble_result                         (valid)
     -> repair_failed_units -> validate_again   (repairable, max 1 pass by default)
     -> mark_partial_or_failed                  (not repairable/exhausted)
  -> write_artifact_if_requested
  -> validate_artifact
  -> emit_response
```

Future extension design may branch before translation for `review_translation` and `sync_bundle`; these modes are not advertised by runtime v0.1.0:

- `review_translation`: align source/target units, run deterministic checks, then use model review only for semantic or style findings.
- `sync_bundle`: diff unit IDs and source hashes, retain approved unchanged targets, and translate only missing/stale targets.

Do not use an open-ended ReAct loop for the main workflow. Explicit nodes and bounded repair edges make cost, tool access, and failure behavior predictable. Let `custom` choose among registered graph capabilities, but keep the same bounded graph and response schema.

## Context and prompts

Store a compact immutable system behavior in `config/agent-context.yaml` inside the image and version it. Build model context from separate, labeled sections:

1. Immutable translator role and source-content isolation rules.
2. Source and target locales.
3. Audience, domain, tone, and locale conventions.
4. Glossary and do-not-translate tokens.
5. Format and placeholder constraints.
6. The current source unit plus only the neighboring context needed for coherence.
7. A minimal structured-output contract.

The system behavior must say that text inside a source unit is untrusted content to translate, not instructions to follow. Require the model to preserve uncertainty rather than inventing source meaning. Avoid including unrelated files or full documents in every unit prompt.

Do not accept replacement system prompts from the caller. Map caller requirements into structured request fields, then let the daemon assemble prompts from the immutable agent context, selected mode contract, structured constraints, source content, relevant tool output, and required response schema.

## Tool policy

Declare and version the allowlist in `config/tools.yaml`. Treat these as deterministic graph/runtime functions, not model-callable tools, and do not include their schemas in model context:

- `read_input(path)`: read within `/work/input`.
- `write_output(path, bytes)`: write within `/work/output` after validation.
- `extract_units(adapter, content)`: parse supported formats.
- `validate_translation(unit, target)`: check placeholders, markup, numbers, whitespace rules, and emptiness.
- `call_translation_model(payload)`: call only the configured OpenAI-compatible base URL.

Do not register a shell, arbitrary Python execution, generic URL fetch, browser, or unrestricted filesystem tool. Treat `custom` as custom translation behavior, not arbitrary agent execution.

## Translation behavior

- Translate rather than answer or summarize source text unless the selected operation explicitly requests transformation.
- Preserve names, numbers, placeholders, ICU/message-format tokens, code spans, URLs, HTML/Markdown structure, and explicit line breaks.
- Use the provided glossary as authoritative; report glossary conflicts instead of choosing silently.
- Maintain cross-unit terminology through a compact translation memory in graph state.
- Use locale-appropriate punctuation and natural phrasing without changing facts or adding content.
- Make deterministic checks first. Use an additional model review only for failed checks, high-quality mode, or explicit review operations.
- Return per-unit failures so one bad unit does not discard a useful batch.

## Provider configuration

Use [providers.md](providers.md) for presets and auth behavior. Require these runtime environment variables:

- `TRANSLATOR_PROVIDER`: `openai`, `nvidia`, `openrouter`, or `custom`.
- `TRANSLATOR_API_BASE_URL`: OpenAI-compatible endpoint base URL.
- `TRANSLATOR_API_KEY`: provider credential; may be blank only when auth mode is `optional` or `none`.
- `TRANSLATOR_MODEL`: model identifier understood by that provider.
- `TRANSLATOR_AUTH_MODE`: `required`, `optional`, or `none`.

Add optional timeout, concurrency, retry, and TLS settings with conservative defaults. Validate that the URL uses HTTPS unless an explicit development flag permits trusted self-hosted HTTP. Redact credentials and authorization headers from all errors and traces.

Use `adaptive-v1` for timeouts. Treat `TRANSLATOR_TIMEOUT_SECONDS` as a base provider timeout, then deterministically scale each provider call from the current batch's source-character count and number of target locales. Estimate the control-socket deadline from mounted input size or inline payload size, batch count, and the maximum permitted repair passes. Cap provider and total job deadlines so malformed or stalled work cannot wait indefinitely. Keep this calculation inside the container; do not ask the translation model to choose a timeout.

When response formatting is `auto`, include all three possible format strategies in the worst-case control deadline. This deadline is an upper bound only; successful strict-schema calls return immediately and do not execute or pay for unused fallbacks.

Implement the compatible API client directly over HTTP. Add `Authorization: Bearer <key>` only when auth mode permits authentication and the key is nonempty. Reject a missing key for `required`; send no Authorization header for blank `optional` or all `none` requests. This ensures unauthenticated self-hosted endpoints work without dummy credentials.

## Token and cost policy

Optimize for inexpensive flash-class models and low repeated context:

- Keep the system prompt short, imperative, and translation-specific. Do not embed full protocol documentation or tool descriptions in it.
- Keep it explicit about complete unit/locale coverage, unchanged IDs, JSON-only output, and prohibited commentary. Prefer a small fixed reliability cost over retries; this overhead becomes negligible in long translation batches.
- Use LangGraph for deterministic routing and state, not for an autonomous ReAct loop.
- Batch units and target locales until a configured character/token budget is reached.
- Split oversized plain-text and Markdown inputs at stable line-preserving boundaries before batching. Scale the effective source-character batch limit by target-locale count so one multilingual response stays bounded.
- Make one model call per batch by default and one targeted repair call only after deterministic failure.
- Treat truncated/incomplete non-streaming HTTP bodies as provider failures eligible for the same single bounded retry. Count the attempt with unknown usage rather than surfacing an untracked runtime exception.
- Spend the single permitted repair call on malformed/non-JSON provider output when needed; otherwise reserve it for deterministic translation-validation failures. Count every provider attempt in `metrics.model_calls`, including malformed responses.
- Resolve placeholders, markup, paths, hashes, parsing, and structural validation without the model.
- Disable model planning, reflection, critic, back-translation, and review in the standard profile.
- Load only glossary entries referenced by the current batch and only minimal neighboring text needed for coherence.
- Keep `isolated` sessions free of previous prompts and source content. In `continue`, retain only bounded glossary/translation-memory pairs, not full conversation history.
- Record provider-reported input/output/total tokens and call counts for every attempt, including malformed responses and repair calls. Mark usage incomplete instead of substituting zero when an endpoint omits usage.
- Use a formatting ladder for compatible endpoints: strict JSON Schema, JSON object mode, then prompt-only JSON. Count both model calls and lower-level provider requests, and persist the strategies used.
- Set a deterministic completion-token budget from current source characters and target count, bounded by `TRANSLATOR_MAX_OUTPUT_TOKENS`, so provider defaults do not truncate otherwise valid long translations.
- For cost experiments and other file workflows, persist a separate versioned `translator-agent/run-v1` record through `output.metrics_path`; never place endpoint URLs, credentials, prompts, or environment values in it.

## Implementation phases

### Phase 1: Protocol and secure skeleton

- Add first-use provider selection, `.transenv` creation from the bundled template, Git-ignore verification, and a stop-for-user-configuration handoff.
- Add Pydantic request/response models and JSON Schema export.
- Add the in-container daemon, Unix-domain control socket, `health`, `capabilities`, and `request` commands, JSON stdin/stdout handling, and stable exit codes.
- Add versioned `agent-context.yaml` and `tools.yaml`; validate both at container startup and report their non-secret versions through capabilities.
- Implement in-container path containment, symlink, overwrite, atomic-write, and secret-redaction tests.
- Add the default runtime Docker image, non-root user, read-only root filesystem compatibility, bounded startup/idle timeouts, graceful shutdown, and direct Docker interaction contract.
- Implement isolated ephemeral and related-work session lifecycles, including session labels, context reset/continue behavior, and cleanup after failures.
- Resolve and pin the Python base image digest and all Python dependencies. Keep the optional Agent Server Dockerfile separate.

### Phase 2: Core translation

- Add the OpenAI-compatible provider adapter and structured output validation.
- Implement `translate_text`, `translate_text_to_file`, and `translate_file_to_file`.
- Add `.txt`, `.md`, `.json`, `.yaml`, and `.csv` adapters.
- Add placeholder/markup/number/glossary validators and a bounded repair edge.

### Phase 3: Advanced operations

- Implement `translate_batch`, `review_translation`, and `sync_bundle`.
- Add source hashes and approved/stale states for incremental localization.
- Constrain `custom` to registered capabilities and schema-validated deliverables.

### Phase 4: Verification and packaging

- Test provider failures, partial batches, prompt injection inside source text, malformed files, path traversal, and idempotent reruns.
- Test lifecycle transitions, readiness timeouts, incompatible capability versions, session isolation, intentional context continuation, changed mounts/configuration, idle shutdown, and cleanup after failures.
- Run container smoke tests against a mock OpenAI-compatible server so CI never needs a real key.
- Run all unit, integration, adapter, and protocol tests inside a test-stage image; require only Docker on the host.
- Add opt-in integration tests for a real endpoint.
- Validate this skill with `quick_validate.py`, then forward-test realistic direct, file, batch, review, and malicious-input requests.

## Acceptance criteria

- Every mode emits the same versioned response envelope.
- The host needs only a compatible Docker client/daemon and explicit input/output paths; no translator runtime or validation code executes on it.
- The caller starts a healthy compatible container before submitting work and obtains capabilities before its first request.
- Ephemeral containers stop and are removed after response capture; session containers are retained only for the same workflow and stop after a bounded idle timeout or explicit cleanup.
- Session context is isolated by default, and continuing context never crosses unrelated tasks or users.
- Direct translation writes nothing.
- File modes cannot read or write outside mounted roots and do not overwrite by default.
- Secrets never appear in state, prompts, stdout, stderr, artifacts, or test snapshots.
- Placeholders and supported document structure survive translation or produce explicit errors.
- Retries and model-review passes are bounded and visible in metrics.
- A failed unit can yield `partial` without losing successful units.
- JSON and YAML bundles round-trip without changing IDs or locale tags.
- The default runtime runs without LangSmith, PostgreSQL, or Redis; the optional Agent Server profile declares those requirements explicitly and runs them as containers or managed services.
