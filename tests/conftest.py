from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from translator_agent.config import AgentAssets, RuntimeConfig


class MockHandler(BaseHTTPRequestHandler):
    authorization: str | None = None
    calls = 0

    def do_POST(self) -> None:
        type(self).authorization = self.headers.get("Authorization")
        type(self).calls += 1
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        prompt = json.loads(request["messages"][1]["content"])
        translations = []
        for unit in prompt["units"]:
            translations.append(
                {
                    "id": unit["id"],
                    "targets": {
                        locale: f"{unit['text']} [{locale}]"
                        for locale in prompt["target_locales"]
                    },
                }
            )
        content = json.dumps(
            {
                "detected_source_locale": "zh-TW",
                "translations": translations,
            },
            ensure_ascii=False,
        )
        response = {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        encoded = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *args: object) -> None:
        return


@pytest.fixture
def mock_provider() -> Iterator[tuple[str, type[MockHandler]]]:
    MockHandler.authorization = None
    MockHandler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", MockHandler
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def runtime_factory(tmp_path: Path):
    def create(base_url: str, *, auth_mode: str = "none", api_key: str = ""):
        input_root = tmp_path / "input"
        output_root = tmp_path / "output"
        input_root.mkdir(exist_ok=True)
        output_root.mkdir(exist_ok=True)
        config_dir = Path(__file__).parents[1] / "config"
        config = RuntimeConfig(
            provider="custom",
            base_url=base_url,
            api_key=api_key,
            model="mock-flash",
            auth_mode=auth_mode,
            allow_insecure_http=True,
            timeout_seconds=5,
            max_retries=1,
            max_batch_chars=12000,
            max_output_tokens=8192,
            response_format_strategy="prompt",
            reasoning_effort="none",
            idle_timeout_seconds=60,
            input_root=input_root,
            output_root=output_root,
            socket_path=tmp_path / "agent.sock",
            config_dir=config_dir,
            session_id="test-session",
            cost_profile="flash",
        )
        return config, AgentAssets.load(config_dir)

    return create
