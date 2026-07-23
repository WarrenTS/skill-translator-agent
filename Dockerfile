FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANGCHAIN_TRACING_V2=false \
    LANGSMITH_TRACING=false \
    TRANSLATOR_CONFIG_DIR=/app/config \
    TRANSLATOR_SOCKET_PATH=/run/translator-agent/agent.sock \
    TRANSLATOR_INPUT_ROOT=/work/input \
    TRANSLATOR_OUTPUT_ROOT=/work/output

WORKDIR /app

RUN addgroup --system --gid 10001 translator \
    && adduser --system --uid 10001 --ingroup translator --home /nonexistent --no-create-home translator \
    && mkdir -p /app/config /run/translator-agent /work/input /work/output \
    && chown -R translator:translator /run/translator-agent /work

COPY pyproject.toml ./
COPY src ./src
COPY config ./config

RUN pip install --no-cache-dir .

USER translator

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["translator-agent", "health"]

ENTRYPOINT ["translator-agent"]
CMD ["serve"]

FROM runtime AS test

USER root
COPY tests ./tests
COPY assets ./assets
RUN pip install --no-cache-dir '.[test]'
USER translator
RUN pytest -p no:cacheprovider
