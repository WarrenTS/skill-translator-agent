from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        prompt = json.loads(request["messages"][1]["content"])
        content = json.dumps(
            {
                "detected_source_locale": "zh-TW",
                "translations": [
                    {
                        "id": unit["id"],
                        "targets": {
                            locale: f"{unit['text']} [{locale}]"
                            for locale in prompt["target_locales"]
                        },
                    }
                    for unit in prompt["units"]
                ],
            },
            ensure_ascii=False,
        )
        body = json.dumps(
            {
                "choices": [{"message": {"content": content}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


if __name__ == "__main__":
    port = int(os.getenv("MOCK_PROVIDER_PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
