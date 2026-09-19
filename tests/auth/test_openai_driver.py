from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from ouro.auth.contracts import AuthError, CredentialRecord
from ouro.auth.credentials import CredentialService
from ouro.auth.driver_contracts import AuthDriver
from ouro.auth.drivers.openai_codex import OpenAICodexDriver
from ouro.auth.drivers.openai_errors import OpenAIOAuthError
from ouro.auth.drivers.openai_tokens import OpenAITokenClient, decode_tokens
from ouro.auth.managed_requests import ManagedRequestAuthenticator, RequestTarget
from ouro.auth.store import FileCredentialStore
from ouro.core.base import new_id
from tests.auth.openai_fixtures import NOW, jwt, tokens

URL = "https://chatgpt.com/backend-api/codex/responses"


def record(**overrides):
    grant = decode_tokens(tokens(), NOW)
    return CredentialRecord(
        **{
            "credential_id": new_id("cred"),
            "driver_id": "openai-codex",
            "revision": 1,
            "state": "active",
            **grant.model_dump(),
            **overrides,
        }
    )


def authenticator(service, credential, driver):
    return ManagedRequestAuthenticator(
        service,
        credential.credential_id,
        driver,
        service_id="codex",
        targets=(RequestTarget("https://chatgpt.com", "/backend-api/codex"),),
        clock=lambda: NOW,
    )


async def test_browser_grant_commit_refresh_snapshot_and_logout(tmp_path):
    requests = []

    def handler(request):
        form = parse_qs(request.content.decode())
        requests.append(form)
        if form["grant_type"] == ["authorization_code"]:
            return httpx.Response(200, json=tokens())
        assert form["refresh_token"] == ["refresh-secret"]
        return httpx.Response(
            200,
            json={"access_token": "rotated-access", "refresh_token": "rotated-refresh"},
        )

    service = CredentialService(FileCredentialStore(tmp_path))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            driver: AuthDriver = OpenAICodexDriver(
                client_id="test-client", client=client, clock=lambda: NOW
            )
            attempt = await driver.begin_authorization("browser")
            try:
                query = parse_qs(urlsplit(str(attempt.challenge.authorization_url)).query)
                async with httpx.AsyncClient(trust_env=False) as local:
                    response = await local.get(
                        "http://127.0.0.1:1455/auth/callback",
                        params={"state": query["state"][0], "code": "code-secret"},
                    )
                    assert response.status_code == 200
                grant = await attempt.wait()
                assert await service.list_credentials() == ()  # The driver cannot commit login.
                saved = await service.commit(
                    new_id("cred"), driver.descriptor.driver_id, grant, expected_revision=0
                )
            finally:
                await attempt.close()

            auth = authenticator(service, saved, driver)
            first = await auth.acquire(URL)
            assert dict(first.headers) == {
                "Authorization": "Bearer access-secret",
                "ChatGPT-Account-Id": "account-secret",
            }
            assert all(await asyncio.gather(*(auth.recover(first) for _ in range(5))))
            renewed = await auth.acquire(URL)
            assert renewed.scope == first.scope == ("codex", "account-secret")
            assert renewed.revision == first.revision + 1
            assert renewed.headers["Authorization"] == "Bearer rotated-access"
            persisted = await service.read(saved.credential_id)
            assert persisted.secret_payload["refresh_token"] == "rotated-refresh"
            assert "id_token" not in persisted.secret_payload
            assert (
                "rotated-refresh"
                not in (await service.status(saved.credential_id)).model_dump_json()
            )
            assert await auth.recover(first)  # Late 401 observes the newer revision.
            assert len(requests) == 2

            await service.logout(saved.credential_id, saved.driver_id)
            with pytest.raises(AuthError) as failure:
                await auth.acquire(URL)
            assert failure.value.code == "authentication_required"
            assert (await service.read(saved.credential_id)).secret_payload == {}
            assert not client.is_closed
    finally:
        await service.close()


@pytest.mark.parametrize(
    "status,body,terminal",
    [
        (401, {"error": "private-secret"}, True),
        (400, {"error": "invalid_grant"}, True),
        (400, {"error": "INVALID_GRANT"}, True),
        (400, {"error": {"code": "refresh_token_expired"}}, True),
        (400, {"error": {"code": "refresh_token_reused"}}, True),
        (400, {"error": {"code": "refresh_token_invalidated"}}, True),
        (403, {"code": "refresh_token_invalidated"}, True),
        (403, {"error": {"code": "Refresh_Token_Expired"}}, True),
        (400, {"error": "invalid_request"}, False),
        (403, {"error": "private-secret"}, False),
        (429, {"error": "private-secret"}, False),
        (500, {"error": "private-secret"}, False),
        (200, {"access_token": "private-secret", "refresh_token": None}, False),
        (200, tokens(id_token=jwt(chatgpt_account_id="another-account")), False),
        (None, {}, False),
    ],
)
async def test_refresh_failures_preserve_material_and_only_rejection_marks_reauth(
    tmp_path, status, body, terminal
):
    calls = []

    def handler(request):
        calls.append(request)
        if status is None:
            raise httpx.ReadError("private-secret", request=request)
        return httpx.Response(status, json=body)

    service = CredentialService(FileCredentialStore(tmp_path))
    try:
        saved = await service.commit(
            new_id("cred"), "openai-codex", decode_tokens(tokens(), NOW), expected_revision=0
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            driver = OpenAICodexDriver(client_id="test-client", client=client)
            auth = authenticator(service, saved, driver)
            first = await auth.acquire(URL)
            with pytest.raises(AuthError) as failure:
                await auth.recover(first)
            assert failure.value.code == (
                "authentication_required" if terminal else "credential_refresh_failed"
            )
            assert "private-secret" not in str(failure.value)
            actual = await service.read(saved.credential_id)
            assert actual.secret_payload == saved.secret_payload
            assert actual.identity == saved.identity
            assert actual.revision == saved.revision + int(terminal)
            assert actual.state == ("reauth_required" if terminal else "active")
            assert len(calls) == 1
    finally:
        await service.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"format_version": 2},
        {"format_version": True},
        {"access_token": "bad\r\nheader"},
        {"refresh_token": None},
        {"account_id": "bad\u007faccount"},
        {"account_id": "other"},
        {"unexpected": "secret"},
    ],
)
async def test_invalid_owned_payload_never_reaches_refresh_http(payload):
    saved = record()
    invalid = saved.model_copy(update={"secret_payload": {**saved.secret_payload, **payload}})

    def must_not_send(request):
        pytest.fail("invalid local material must fail before network I/O")

    async with httpx.AsyncClient(transport=httpx.MockTransport(must_not_send)) as client:
        driver = OpenAICodexDriver(client_id="test-client", client=client)
        with pytest.raises(AuthError) as encode_error:
            driver.encode(invalid)
        with pytest.raises(AuthError) as refresh_error:
            await driver.refresh(invalid)
        assert encode_error.value.code == refresh_error.value.code == "authentication_required"


def test_missing_payload_version_wrong_driver_and_inactive_records_fail_closed():
    driver = OpenAICodexDriver(client_id="test-client")
    saved = record()
    for state in ("logged_out", "reauth_required"):
        with pytest.raises(AuthError) as failure:
            driver.encode(saved.model_copy(update={"state": state}))
        assert failure.value.code == "authentication_required"
    with pytest.raises(AuthError) as failure:
        driver.encode(saved.model_copy(update={"driver_id": "another-driver"}))
    assert failure.value.code == "credential_conflict"
    payload = dict(saved.secret_payload)
    payload.pop("format_version")
    with pytest.raises(AuthError) as failure:
        driver.encode(saved.model_copy(update={"secret_payload": payload}))
    assert failure.value.code == "authentication_required"


async def test_only_verified_browser_method_is_exposed():
    driver = OpenAICodexDriver(client_id="test-client")
    assert [m.method_id for m in driver.descriptor.methods] == ["browser"]
    with pytest.raises(AuthError) as failure:
        await driver.begin_authorization("device")
    assert failure.value.code == "authorization_unsupported"


@pytest.mark.parametrize("end", ["denied", "expired", "cancelled", "http_error"])
async def test_browser_failures_release_port_and_keep_borrowed_client(end):
    def handler(request):
        return httpx.Response(500, text="token-secret callback-secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        driver = OpenAICodexDriver(
            client_id="test-client",
            client=client,
            authorization_timeout=0.05 if end == "expired" else 60,
        )
        attempt = await driver.begin_authorization("browser")
        try:
            if end in {"denied", "http_error"}:
                query = parse_qs(urlsplit(str(attempt.challenge.authorization_url)).query)
                params = {"state": query["state"][0]}
                params["error" if end == "denied" else "code"] = "callback-secret"
                async with httpx.AsyncClient(trust_env=False) as local:
                    await local.get("http://127.0.0.1:1455/auth/callback", params=params)
            if end == "cancelled":
                await attempt.cancel()
            with pytest.raises(AuthError) as failure:
                await attempt.wait()
            assert failure.value.code == (
                "authorization_failed" if end == "http_error" else "authorization_" + end
            )
            assert "secret" not in str(failure.value)
            if end == "http_error":
                assert isinstance(failure.value, OpenAIOAuthError)
                assert failure.value.status == 500
        finally:
            await attempt.close()
        assert not client.is_closed
        second = await driver.begin_authorization("browser")
        await second.close()


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_request_scoped_http_is_closed_on_every_exit(monkeypatch, outcome):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        if outcome == "cancel":
            await asyncio.Event().wait()
        if outcome == "failure":
            raise httpx.ConnectError("private-secret", request=request)
        return httpx.Response(200, json=tokens())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", lambda: client)
    token_client = OpenAITokenClient(client_id="test-client")
    task = asyncio.create_task(
        token_client.exchange("code", "verifier", "http://localhost:1455/auth/callback")
    )
    await entered.wait()
    if outcome == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif outcome == "failure":
        with pytest.raises(OpenAIOAuthError):
            await task
    else:
        assert (await task).identity.account_id == "account-secret"
    assert client.is_closed


@pytest.mark.parametrize("cancel", [False, True])
async def test_pending_browser_exchange_is_stopped_without_closing_borrowed_http(cancel):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        driver = OpenAICodexDriver(
            client_id="test-client",
            client=client,
            authorization_timeout=60 if cancel else 0.2,
        )
        attempt = await driver.begin_authorization("browser")
        try:
            query = parse_qs(urlsplit(str(attempt.challenge.authorization_url)).query)
            async with httpx.AsyncClient(trust_env=False) as local:
                response = await local.get(
                    "http://127.0.0.1:1455/auth/callback",
                    params={"state": query["state"][0], "code": "code-secret"},
                )
                assert response.status_code == 200
            await entered.wait()
            if cancel:
                await attempt.cancel()
            with pytest.raises(AuthError) as failure:
                await attempt.wait()
            assert failure.value.code == (
                "authorization_cancelled" if cancel else "authorization_expired"
            )
        finally:
            await attempt.close()
        assert stopped.is_set() and not client.is_closed
        next_attempt = await driver.begin_authorization("browser")
        await next_attempt.close()
