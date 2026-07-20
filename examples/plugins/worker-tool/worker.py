from __future__ import annotations

import json
import sys


def respond(request_id: int, result: dict[str, object]) -> None:
    print(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
        flush=True,
    )


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    result: dict[str, object] = {}
    if method == "initialize":
        result = {"protocol_version": "1.1"}
    elif method == "contributions.prepare":
        result = {
            "tools": {
                "echo": {
                    "parameters_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                        "additionalProperties": False,
                    }
                }
            }
        }
    elif method == "tool.call":
        params = request.get("params", {})
        call = params.get("call", {}) if isinstance(params, dict) else {}
        arguments = call.get("arguments", {}) if isinstance(call, dict) else {}
        value = arguments.get("value", "") if isinstance(arguments, dict) else ""
        result = {"content": str(value), "structured": {"value": value}}
    if isinstance(request.get("id"), int):
        respond(request["id"], result)
    if method == "shutdown":
        break
