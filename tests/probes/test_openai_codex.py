from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from dev.probes import openai_codex
from dev.probes.codex_oauth import ProbeError
from ouro.auth.drivers.openai_errors import OpenAIOAuthError


def test_live_network_requires_explicit_opt_in_method_and_model():
    for arguments in ([], ["--method", "device", "--model", "model"], ["--live"]):
        with pytest.raises(SystemExit) as failure:
            openai_codex.parser().parse_args(arguments)
        assert failure.value.code == 2


async def test_failed_live_request_emits_one_safe_report(monkeypatch, capsys):
    async def fail(self):
        raise ProbeError("auth_http_error", status=403)

    monkeypatch.setattr(openai_codex.CodexOAuthProbe, "device", fail)
    report, code = await openai_codex.run(
        argparse.Namespace(
            method="device",
            model="test-model",
            open_browser=False,
        )
    )
    assert code == 1 and report["status"] == "failed"
    assert report["stage"] == "authorization" and report["http_status"] == 403
    assert report["completed_steps"] == [] and report["production_support"] is False
    assert capsys.readouterr().out == ""


async def test_unexpected_exception_cannot_print_secrets_or_traceback(monkeypatch):
    async def fail(self):
        raise RuntimeError("access-secret callback-secret")

    monkeypatch.setattr(openai_codex.CodexOAuthProbe, "device", fail)
    report, code = await openai_codex.run(
        argparse.Namespace(
            method="device",
            model="test-model",
            open_browser=False,
        )
    )
    assert code == 1 and report["error_code"] == "probe_internal_error"
    assert "secret" not in json.dumps(report)


async def test_shared_browser_driver_failure_keeps_safe_diagnostic(monkeypatch):
    async def fail(self):
        raise OpenAIOAuthError("callback_port_unavailable")

    monkeypatch.setattr(openai_codex.CodexOAuthProbe, "browser", fail)
    report, code = await openai_codex.run(
        argparse.Namespace(method="browser", model="test-model", open_browser=False)
    )
    assert code == 1
    assert report["error_code"] == "callback_port_unavailable"
    assert report["http_status"] is None
    assert report["stage"] == "authorization"


@pytest.mark.parametrize("network_failure", [False, True])
async def test_authorization_poll_failure_keeps_safe_cause(monkeypatch, network_failure):
    def handler(incoming):
        if incoming.url.path.endswith("usercode"):
            return httpx.Response(
                200,
                json={
                    "device_auth_id": "device-secret",
                    "user_code": "CODE",
                    "interval": "5",
                },
            )
        if network_failure:
            raise httpx.ReadError("token-secret callback-secret", request=incoming)
        return httpx.Response(500, text="token-secret callback-secret")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(openai_codex.httpx, "AsyncClient", lambda **kwargs: client)
    report, code = await openai_codex.run(
        argparse.Namespace(
            method="device",
            model="test-model",
            open_browser=False,
        )
    )
    assert code == 1 and client.is_closed
    assert report["stage"] == "authorization"
    assert report["error_code"] == ("auth_network_error" if network_failure else "auth_http_error")
    assert report["http_status"] == (None if network_failure else 500)
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("stream_items", [False, True])
async def test_cli_runs_the_full_memory_only_experiment_and_closes_http(
    monkeypatch, capsys, stream_items
):
    payload = base64.urlsafe_b64encode(b'{"chatgpt_account_id":"account-secret"}').decode()
    verifier = "verifier-secret"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    models = 0

    def handler(incoming):
        nonlocal models
        if incoming.url.path.endswith("usercode"):
            return httpx.Response(
                200, json={"device_auth_id": "device-secret", "user_code": "CODE", "interval": "5"}
            )
        if incoming.url.path.endswith("deviceauth/token"):
            return httpx.Response(
                200,
                json={
                    "authorization_code": "code-secret",
                    "code_verifier": verifier,
                    "code_challenge": challenge,
                },
            )
        if incoming.url.path.endswith("oauth/token"):
            progress = capsys.readouterr()
            assert progress.out == ""
            if b"grant_type=refresh_token" in incoming.content:
                assert "[2/3] Authorization complete" in progress.err
            else:
                assert "[1/3] Waiting for authorization and token exchange" in progress.err
            return httpx.Response(
                200,
                json={
                    "access_token": "access-secret",
                    "refresh_token": "refresh-secret",
                    "id_token": "h." + payload + ".s",
                },
            )
        models += 1
        if models == 1:
            progress = capsys.readouterr()
            assert progress.out == ""
            assert "[3/3] Refresh complete; verifying four model requests" in progress.err
            assert "Verification passed" not in progress.err
        if models in (2, 3):
            output = [
                {
                    "type": "function_call",
                    "call_id": f"call-{models}",
                    "name": "ouro_probe_echo",
                    "arguments": json.dumps({"round": models - 1}),
                }
            ]
        else:
            output = [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "OURO_OK" if models == 1 else "OURO_TOOLS_OK",
                        }
                    ],
                }
            ]
        terminal = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [] if stream_items else output,
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            },
        }
        events = []
        if stream_items:
            events.extend(
                {"type": "response.output_item.done", "output_index": index, "item": item}
                for index, item in enumerate(output)
            )
        events.append(terminal)
        return httpx.Response(
            200, content="".join("data: " + json.dumps(event) + "\n\n" for event in events)
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(openai_codex.httpx, "AsyncClient", lambda **kwargs: client)
    report, code = await openai_codex.run(
        argparse.Namespace(
            method="device",
            model="test-model",
            open_browser=False,
        )
    )
    assert client.is_closed and code == 0 and models == 4
    assert report["completed_steps"] == ["authorization", "refresh", "responses"]
    assert report["status"] == "passed" and report["expiry_provided"] is False
    assert "secret" not in json.dumps(report)
    captured = capsys.readouterr()
    assert captured.out == "" and "Verification passed" in captured.err


async def test_cli_cancellation_closes_pending_authorization_and_http(monkeypatch):
    polling = asyncio.Event()

    async def handler(incoming):
        if incoming.url.path.endswith("usercode"):
            return httpx.Response(
                200, json={"device_auth_id": "id", "user_code": "CODE", "interval": "5"}
            )
        polling.set()
        await asyncio.Event().wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(openai_codex.httpx, "AsyncClient", lambda **kwargs: client)
    task = asyncio.create_task(
        openai_codex.run(
            argparse.Namespace(
                method="device",
                model="test-model",
                open_browser=False,
            )
        )
    )
    await polling.wait()
    task.cancel()
    report, code = await task
    assert code == 130 and report["status"] == "cancelled"
    assert report["completed_steps"] == [] and client.is_closed


def test_probe_cannot_read_production_configuration_sessions_or_credential_caches():
    root = Path(__file__).resolve().parents[2]
    for path in (root / "dev/probes").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(
            module
            and module.startswith(
                (
                    "ouro.config",
                    "ouro.sessions",
                    "ouro.application",
                    "ouro.local_application",
                    "ouro.auth.store",
                    "ouro.auth.credentials",
                )
            )
            for module in imports
        )
    for path in (root / "ouro").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("dev.probes")
            elif isinstance(node, ast.Import):
                assert not any(alias.name.startswith("dev.probes") for alias in node.names)
