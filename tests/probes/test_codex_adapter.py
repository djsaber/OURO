from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from dev.probes import openai_codex
from dev.probes.codex_adapter import ProbeAuthentication, verify_adapter
from dev.probes.codex_oauth import ProbeError
from ouro.auth.contracts import AuthError
from ouro.auth.driver_contracts import BrowserAuthorizationChallenge
from ouro.auth.drivers.openai_tokens import decode_tokens
from tests.auth.openai_fixtures import tokens
from tests.providers.responses_fixtures import call, item_event, message, reasoning, terminal


def grant(access="access-secret"):
    return decode_tokens(tokens(access_token=access), datetime.now(UTC))


def sse(items):
    events = [item_event(index, item) for index, item in enumerate(items)] + [terminal([])]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
    )


async def test_formal_adapter_replays_encrypted_reasoning_across_refresh_and_two_tools():
    requests = []
    refreshed = False

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        number = len(requests)
        assert request.headers["authorization"] == (
            "Bearer rotated-secret" if refreshed else "Bearer access-secret"
        )
        assert request.headers["chatgpt-account-id"] == "account-secret"
        assert "max_output_tokens" not in payload
        assert payload["store"] is False and "truncation" not in payload
        assert "previous_response_id" not in payload
        if number == 1:
            assert not refreshed
            return sse([reasoning(), message("OURO_OK")])
        assert refreshed
        assert payload["input"][1:3] == [reasoning(), message("OURO_OK")]
        if number in (2, 3):
            assert payload["tools"][0]["strict"] is False
            return sse([call(number - 1, name="ouro_probe_echo")])
        tool_results = [i for i in payload["input"] if i.get("type") == "function_call_output"]
        assert [json.loads(i["output"]) for i in tool_results] == [
            {"round": 1, "ok": True},
            {"round": 2, "ok": True},
        ]
        return sse([message("OURO_TOOLS_OK", item_id="final")])

    async def refresh(current):
        nonlocal refreshed
        assert len(requests) == 1
        assert current.secret_payload["access_token"] == "access-secret"
        refreshed = True
        return grant("rotated-secret")

    observations = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await verify_adapter(
            client,
            grant(),
            model="fixture",
            max_output_tokens=512,
            refresh=refresh,
            observations=observations,
        )
        assert not client.is_closed
        assert client.event_hooks["response"] == []
    assert len(requests) == 4 and all(o["validated"] for o in observations)
    assert observations[-1]["scope_stable_after_refresh"] is True
    assert observations[-1]["encrypted_reasoning_replayed"] is True
    assert observations[0]["usage"]["input_tokens"] == 20
    assert "secret" not in json.dumps(observations)
    assert "opaque-ciphertext" not in json.dumps(observations)
    assert "OURO_OK" not in json.dumps(observations)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
async def test_adapter_rejection_is_safe_and_does_not_retry(status):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        assert "max_output_tokens" not in json.loads(request.content)
        return httpx.Response(status, text="access-secret account-secret raw-service-error")

    async def refresh(current):
        pytest.fail("A rejected first response must not silently refresh or continue")

    observations = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProbeError) as error:
            await verify_adapter(
                client,
                grant(),
                model="fixture",
                max_output_tokens=8192,
                refresh=refresh,
                observations=observations,
            )
    assert error.value.status == status
    assert calls == 1 and observations[0]["validated"] is False
    assert "secret" not in str(error.value) + json.dumps(observations)


@pytest.mark.parametrize(
    ("body", "diagnostic"),
    [
        (
            {
                "error": {
                    "param": "max_output_tokens",
                    "code": "unsupported_parameter",
                    "message": "access-secret account-secret",
                }
            },
            {"parameter": "max_output_tokens", "reason": "unsupported_parameter"},
        ),
        (
            {"detail": "Unsupported parameter: max_output_tokens"},
            {"parameter": "max_output_tokens", "reason": "unsupported_parameter"},
        ),
        (
            {"error": {"message": "Unsupported parameter: truncation"}},
            {"parameter": "truncation", "reason": "unsupported_parameter"},
        ),
        ({"error": {"param": "account-secret", "code": "secret", "message": "secret"}}, {}),
        ({"detail": "Unsupported parameter: max_output_tokens access-secret"}, {}),
        ({"error": {"param": ["max_output_tokens"], "code": {"secret": True}}}, {}),
        ("<html>access-secret max_output_tokens</html>", {}),
    ],
)
async def test_adapter_reports_only_allowlisted_request_rejection_facts(body, diagnostic):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert "max_output_tokens" not in payload and "truncation" not in payload
        return (
            httpx.Response(400, json=body)
            if isinstance(body, dict)
            else httpx.Response(400, text=body)
        )

    async def refresh(current):
        pytest.fail("rejected request must stop before refresh")

    observations = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hooks = list(client.event_hooks["response"])
        with pytest.raises(ProbeError) as error:
            await verify_adapter(
                client,
                grant(),
                model="fixture",
                max_output_tokens=8192,
                refresh=refresh,
                observations=observations,
            )
        assert client.event_hooks["response"] == hooks
    assert calls == 1 and error.value.status == 400
    assert observations[0]["request_error"] == diagnostic
    assert "secret" not in json.dumps(observations)


async def test_adapter_probe_cancellation_closes_the_active_http_stream():
    entered, closed = asyncio.Event(), asyncio.Event()

    class WaitingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    async def refresh(current):
        pytest.fail("cancelled request must not refresh")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=WaitingStream()))
    ) as client:
        task = asyncio.create_task(
            verify_adapter(
                client,
                grant(),
                model="fixture",
                max_output_tokens=8192,
                refresh=refresh,
                observations=[],
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and not client.is_closed
        assert client.event_hooks["response"] == []


async def test_probe_authentication_rejects_other_targets():
    async with httpx.AsyncClient() as client:
        auth = ProbeAuthentication(client, grant())
        with pytest.raises(AuthError, match="target"):
            await auth.acquire("https://elsewhere.invalid/responses")


@pytest.mark.parametrize("rejected", [False, True])
async def test_adapter_cli_runs_formal_path_and_reports_local_budget_only(
    monkeypatch, capsys, rejected
):
    closed = []
    model_calls = []

    class Attempt:
        challenge = BrowserAuthorizationChallenge(
            authorization_url="https://fixture.invalid/login",
            expires_at=datetime.now(UTC),
        )

        async def wait(self):
            return grant()

        async def close(self):
            closed.append(True)

    async def browser(self):
        return Attempt()

    def handler(request):
        if request.url.path.endswith("oauth/token"):
            assert len(model_calls) == 1
            return httpx.Response(
                200,
                json=tokens(
                    access_token="rotated-secret",
                    refresh_token="rotated-refresh-secret",
                ),
            )
        assert "max_output_tokens" not in json.loads(request.content)
        model_calls.append(request)
        if rejected:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "param": "store",
                        "code": "unsupported_parameter",
                        "message": "secret",
                    },
                },
            )
        number = len(model_calls)
        if number in (2, 3):
            return sse([call(number - 1, name="ouro_probe_echo")])
        return sse([reasoning(), message("OURO_OK" if number == 1 else "OURO_TOOLS_OK")])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(openai_codex.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(openai_codex.CodexOAuthProbe, "browser", browser)
    report, code = await openai_codex.run(
        argparse.Namespace(
            adapter=True,
            method="browser",
            model="fixture",
            open_browser=False,
            max_output_tokens=2048,
        )
    )
    if rejected:
        assert code == 1 and report["http_status"] == 400
        assert report["completed_steps"] == ["authorization"] and report["stage"] == "responses"
        assert report["responses"][0]["validated"] is False and len(model_calls) == 1
        assert report["responses"][0]["request_error"] == {
            "parameter": "store",
            "reason": "unsupported_parameter",
        }
    else:
        assert code == 0 and report["completed_steps"] == ["authorization", "refresh", "responses"]
        assert report["refresh_token_rotated"] is True
        assert len(model_calls) == 4 and all(o["validated"] for o in report["responses"])
    assert report["request_path"] == "production_adapter"
    assert report["max_output_tokens"] == 2048
    assert report["output_limit_enforced"] is False
    assert "local planning budget only" in capsys.readouterr().err
    assert client.is_closed and closed == [True]
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize(("adapter", "limit"), [(False, 1024), (True, 0), (True, 128001)])
async def test_invalid_adapter_options_fail_before_authorization(adapter, limit, monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("invalid options must fail before creating an HTTP client")

    monkeypatch.setattr(openai_codex.httpx, "AsyncClient", forbidden)
    report, code = await openai_codex.run(
        argparse.Namespace(
            adapter=adapter,
            method="browser",
            model="fixture",
            open_browser=False,
            max_output_tokens=limit,
        )
    )
    assert code == 1 and report["completed_steps"] == []
