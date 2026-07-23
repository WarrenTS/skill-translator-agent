from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any

from translator_agent.config import AgentAssets, RuntimeConfig
from translator_agent.server import serve
from translator_agent.timeouts import control_timeout_seconds


def main() -> int:
    parser = argparse.ArgumentParser(prog="translator-agent")
    parser.add_argument(
        "command", choices=("serve", "health", "capabilities", "request")
    )
    args = parser.parse_args()
    config = RuntimeConfig.from_env()
    if args.command == "serve":
        assets = AgentAssets.load(config.config_dir)
        return serve(config, assets)
    payload: Any = None
    if args.command == "request":
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            _print_json(
                {
                    "status": "failed",
                    "errors": [
                        {
                            "code": "INVALID_JSON",
                            "message": str(exc),
                            "retryable": False,
                        }
                    ],
                }
            )
            return 2
    response = _send(
        config.socket_path,
        {"command": args.command, "payload": payload},
        timeout_seconds=_control_timeout_seconds(config, payload),
    )
    _print_json(response)
    if args.command in {"health", "capabilities"}:
        return 0 if response.get("ready") is True else 1
    return 0 if response.get("status") in {"succeeded", "partial"} else 4


def _control_timeout_seconds(config: RuntimeConfig, payload: Any = None) -> int:
    return control_timeout_seconds(config, payload)


def _send(
    socket_path: Any,
    envelope: dict[str, Any],
    *,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(socket_path))
            client.sendall(
                json.dumps(
                    envelope, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
                + b"\n"
            )
            stream = client.makefile("rb")
            line = stream.readline(10_000_001)
        if not line or len(line) > 10_000_000:
            raise RuntimeError("invalid or oversized daemon response")
        response = json.loads(line.decode("utf-8"))
        if not isinstance(response, dict):
            raise RuntimeError("daemon response is not a JSON object")
        return response
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        return {
            "status": "failed",
            "errors": [
                {
                    "code": "CONTROL_SOCKET_ERROR",
                    "message": str(exc),
                    "retryable": True,
                }
            ],
        }


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
