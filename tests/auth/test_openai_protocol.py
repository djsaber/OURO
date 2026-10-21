from __future__ import annotations

import asyncio
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ouro.auth.contracts import AuthError
from ouro.auth.drivers.openai_callback import CallbackReceiver
from ouro.auth.drivers.openai_codex import OpenAICodexDriver
from ouro.auth.drivers.openai_errors import OpenAIOAuthError
from ouro.auth.drivers.openai_tokens import ISSUER, OpenAITokenClient, decode_tokens
from tests.auth.openai_fixtures import NOW, jwt, tokens

CLIENT_ID = "test-client"


def test_token_rotation_retention_and_stable_routing():
    first = decode_tokens(tokens(), NOW)
    rotated = decode_tokens({"access_token": "new", "refresh_token": "rotated"}, NOW, first)
    retained = decode_tokens({"access_token": "again"}, NOW, rotated)
    assert rotated.identity == first.identity == retained.identity
    assert (
        rotated.secret_payload["refresh_token"]
        == retained.secret_payload["refresh_token"]
        == "rotated"
    )
    assert first.expires_at is not None and first.expires_at.timestamp() == NOW.timestamp() + 3600
    assert retained.expires_at is None  # Do not invent a lifetime from upstream defaults.
    assert "access-secret" not in repr(first)
    assert "refresh-secret" not in repr(first)


@pytest.mark.parametrize(
    "data,code",
    [
        (tokens(access_token=""), "invalid_response"),
        (tokens(refresh_token=None), "invalid_response"),
        ({"access_token": "x"}, "missing_refresh_token"),
        (tokens(expires_in=True), "invalid_token_expiry"),
        (tokens(expires_in=-1), "invalid_token_expiry"),
        (tokens(id_token=jwt(organizations=[{"id": "wrong"}])), "ambiguous_account_route"),
        (
            tokens(id_token=jwt(chatgpt_account_id="a", chatgpt_compute_residency="eu")),
            "unsupported_residency",
        ),
        (tokens(access_token=jwt(chatgpt_account_id="other")), "ambiguous_account_route"),
    ],
)
def test_invalid_tokens_are_rejected_without_secret_diagnostics(data, code):
    with pytest.raises(OpenAIOAuthError) as failure:
        decode_tokens(data, NOW)
    assert failure.value.reason == code
    assert "secret" not in str(failure.value)


def test_refresh_cannot_switch_account():
    first = decode_tokens(tokens(), NOW)
    with pytest.raises(OpenAIOAuthError) as failure:
        decode_tokens(tokens(id_token=jwt(chatgpt_account_id="different")), NOW, first)
    assert failure.value.reason == "ambiguous_account_route"


async def test_loopback_ignores_wrong_state_method_path_and_duplicate_callbacks():
    receiver = CallbackReceiver("expected")
    await receiver.start(0)
    port = receiver.port
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            base = f"http://127.0.0.1:{port}"
            for path in (
                "/unrelated?state=expected&code=bad",
                "/auth/callback?state=wrong&error=access_denied",
                "/auth/callback?state=expected&state=other&code=bad",
                "/auth/callback?state=wrong&code=bad",
                "/auth/callback?state=expected&code=a&code=b",
            ):
                assert (await client.get(base + path)).status_code == 400
                assert not receiver.code.done()
            assert (
                await client.post(base + "/auth/callback?state=expected&code=bad")
            ).status_code == 400
            assert not receiver.code.done()
            response = await client.get(base + "/auth/callback?state=expected&code=valid-secret")
            assert response.status_code == 200 and await receiver.code == "valid-secret"
            assert "valid-secret" not in response.text
            assert (
                await client.get(base + "/auth/callback?state=expected&code=late")
            ).status_code == 409
    finally:
        await receiver.close()
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", port)
    server.close()
    await server.wait_closed()


async def test_loopback_denial_and_idle_connections_are_closed():
    receiver = CallbackReceiver("expected")
    await receiver.start(0)
    reader, writer = await asyncio.open_connection("127.0.0.1", receiver.port)
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(
            f"http://127.0.0.1:{receiver.port}/auth/callback?state=expected&error=private-reason"
        )
        assert response.status_code == 200
    with pytest.raises(AuthError) as failure:
        await receiver.code
    assert failure.value.code == "authorization_denied"
    await receiver.close()
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()


async def test_browser_pkce_exchange_and_port_cleanup(openai_callback):
    captured = []

    def handler(request):
        assert str(request.url) == ISSUER + "/oauth/token"
        captured.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json=tokens())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth = OpenAICodexDriver(client_id=CLIENT_ID, client=client)
        attempt = await oauth.begin_authorization("browser")
        query = parse_qs(urlsplit(str(attempt.challenge.authorization_url)).query)
        assert query["client_id"] == [CLIENT_ID]
        assert query["scope"] == ["openid profile email offline_access"]
        assert query["originator"] == ["ouro"]
        try:
            async with httpx.AsyncClient(trust_env=False) as local:
                response = await local.get(
                    openai_callback(),
                    params={
                        "state": query["state"][0],
                        "code": "authorization-secret",
                    },
                )
                assert response.status_code == 200
            grant = await attempt.wait()
            assert grant.secret_payload["access_token"] == "access-secret"
            form = captured[0]
            assert form["redirect_uri"] == ["http://localhost:1455/auth/callback"]
            digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
            assert query["code_challenge"] == [
                base64.urlsafe_b64encode(digest).decode().rstrip("=")
            ]
        finally:
            await attempt.close()
        # A second pending login can bind immediately and cancellation frees it too.
        second = await oauth.begin_authorization("browser")
        await second.cancel()
        third = await oauth.begin_authorization("browser")
        await third.close()


async def test_browser_port_conflict_is_reported_without_interfering_with_owner(openai_callback):
    receiver = CallbackReceiver("other-client")
    await receiver.start(1455)
    try:
        async with httpx.AsyncClient() as client:
            with pytest.raises(OpenAIOAuthError) as failure:
                await OpenAICodexDriver(client_id=CLIENT_ID, client=client).begin_authorization(
                    "browser"
                )
            assert failure.value.reason == "callback_port_unavailable"
        assert not receiver.code.done()
    finally:
        await receiver.close()


@pytest.mark.parametrize("status", [302, 400, 401, 403, 429, 500])
async def test_token_failures_never_redirect_retry_or_expose_response(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status, text="access-secret refresh-secret", headers={"location": "https://evil.test/"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(OpenAIOAuthError) as failure:
            await OpenAITokenClient(client_id=CLIENT_ID, client=client).refresh(
                decode_tokens(tokens(), NOW)
            )
    assert failure.value.status == status
    assert "secret" not in str(failure.value)
    assert len(requests) == 1
