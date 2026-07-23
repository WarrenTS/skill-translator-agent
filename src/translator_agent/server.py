from __future__ import annotations

import json
import os
import signal
import socketserver
import threading
import time
from typing import Any

from pydantic import ValidationError

from translator_agent import __version__
from translator_agent.config import AgentAssets, RuntimeConfig
from translator_agent.graph import TranslatorGraph
from translator_agent.models import TranslationRequest
from translator_agent.session import SessionState
from translator_agent.timeouts import TIMEOUT_POLICY_ID


SUPPORTED_MODES = {
    "translate_text",
    "translate_text_to_file",
    "translate_file_to_file",
    "custom",
    "translate_batch",
}


class TranslatorRuntime:
    def __init__(self, config: RuntimeConfig, assets: AgentAssets) -> None:
        self.config = config
        self.assets = assets
        self.session = SessionState(config.session_id)
        self.graph = TranslatorGraph(config, assets, self.session)
        self._invoke_lock = threading.Lock()

    def health(self) -> dict[str, Any]:
        return {
            "schema_version": "translator-agent/control-v1",
            "ready": True,
            "runtime_version": __version__,
            "session_id": self.config.session_id,
        }

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": "translator-agent/control-v1",
            "runtime_version": __version__,
            "ready": True,
            "protocol_versions": ["translator-agent/v1"],
            "supported_modes": sorted(SUPPORTED_MODES),
            "input_formats": ["text", "markdown", "json", "yaml", "csv"],
            "output_formats": ["text", "json", "yaml", "csv", "source"],
            "agent_context_version": self.assets.context_version,
            "tools_version": self.assets.tools_version,
            "tool_ids": list(self.assets.tool_ids),
            "provider_ready": self.config.provider_ready,
            "provider": self.config.provider,
            "model": self.config.model or None,
            "configuration_errors": self.config.validation_errors(),
            "session_id": self.config.session_id,
            "cost_profile": self.config.cost_profile,
            "timeout_policy": TIMEOUT_POLICY_ID,
            "response_format_strategy": self.config.response_format_strategy,
            "max_output_tokens": self.config.max_output_tokens,
            "reasoning_effort": self.config.reasoning_effort,
        }

    def request(self, payload: Any) -> dict[str, Any]:
        try:
            request = TranslationRequest.model_validate(payload)
        except ValidationError as exc:
            request_id = payload.get("request_id", "unknown") if isinstance(payload, dict) else "unknown"
            return _error_response(
                request_id,
                "unknown",
                "REQUEST_SCHEMA_ERROR",
                exc.errors(include_url=False, include_input=False),
            )
        if request.session.id != self.config.session_id:
            return _error_response(
                request.request_id,
                request.mode,
                "SESSION_MISMATCH",
                "request session ID does not match the active container",
            )
        if request.mode not in SUPPORTED_MODES:
            return _error_response(
                request.request_id,
                request.mode,
                "MODE_NOT_IMPLEMENTED",
                f"mode is not available in runtime {__version__}",
            )
        if not self.config.provider_ready:
            return _error_response(
                request.request_id,
                request.mode,
                "PROVIDER_CONFIGURATION_ERROR",
                self.config.validation_errors(),
            )
        sequence = self.session.begin_request()
        with self._invoke_lock:
            try:
                return self.graph.invoke(request, sequence)
            except Exception as exc:
                return _error_response(
                    request.request_id,
                    request.mode,
                    "RUNTIME_ERROR",
                    str(exc),
                    session_id=self.config.session_id,
                    sequence=sequence,
                )


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(10_000_001)
        if not line or len(line) > 10_000_000:
            self._write({"status": "failed", "error": "request exceeds 10 MB"})
            return
        try:
            envelope = json.loads(line.decode("utf-8"))
            command = envelope.get("command")
            runtime: TranslatorRuntime = self.server.runtime  # type: ignore[attr-defined]
            if command == "health":
                response = runtime.health()
            elif command == "capabilities":
                response = runtime.capabilities()
            elif command == "request":
                response = runtime.request(envelope.get("payload"))
            else:
                response = {"status": "failed", "error": "unknown command"}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            response = {"status": "failed", "error": f"invalid control JSON: {exc}"}
        self._write(response)

    def _write(self, payload: dict[str, Any]) -> None:
        self.wfile.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


def serve(config: RuntimeConfig, assets: AgentAssets) -> int:
    socket_path = config.socket_path
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    runtime = TranslatorRuntime(config, assets)
    server = _UnixServer(str(socket_path), _Handler)
    server.runtime = runtime  # type: ignore[attr-defined]
    server.timeout = 1.0
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            server.handle_request()
            idle = time.monotonic() - runtime.session.last_activity
            if idle >= config.idle_timeout_seconds:
                break
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)
    return 0


def _error_response(
    request_id: str,
    mode: str,
    code: str,
    detail: Any,
    *,
    session_id: str | None = None,
    sequence: int | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "translator-agent/v1",
        "request_id": request_id,
        "status": "failed",
        "mode": mode,
        "runtime": {
            "session_id": session_id,
            "request_sequence": sequence,
            "context_reused": False,
        },
        "resolved_source_locale": "und",
        "result": {},
        "artifacts": [],
        "warnings": [],
        "errors": [
            {
                "code": code,
                "message": detail,
                "retryable": False,
            }
        ],
        "metrics": {
            "unit_count": 0,
            "model_calls": 0,
            "provider_requests": 0,
            "format_strategy_requests": {},
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "provider_reported_cost": None,
            "usage_reported_attempts": 0,
            "usage_missing_attempts": 0,
            "usage_complete": False,
        },
    }
