from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from dev.probes.codex_oauth import CLIENT_ID, CodexOAuthProbe, ProbeError
from ouro.auth.contracts import AuthError
from ouro.auth.drivers.openai_tokens import ISSUER
from tests.auth.openai_fixtures import tokens


async def test_device_pending_poll_exchange_and_refresh_use_fixed_targets():
    requests, sleeps = [], []
    pending = iter([403, 404, 200])
    verifier = "device-verifier-secret"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/usercode"):
            assert json.loads(request.content) == {"client_id": CLIENT_ID}
            return httpx.Response(
                200, json={"device_auth_id": "device-secret", "usercode": "CODE", "interval": "7"}
            )
        if request.url.path.endswith("deviceauth/token"):
            assert json.loads(request.content) == {
                "device_auth_id": "device-secret",
                "user_code": "CODE",
            }
            return httpx.Response(
                next(pending),
                json={
                    "authorization_code": "code-secret",
                    "code_verifier": verifier,
                    "code_challenge": challenge,
                },
            )
        form = parse_qs(request.content.decode())
        if form["grant_type"] == ["authorization_code"]:
            assert form["redirect_uri"] == [ISSUER + "/deviceauth/callback"]
            return httpx.Response(200, json=tokens())
        assert form["refresh_token"] == ["refresh-secret"]
        return httpx.Response(
            200, json={"access_token": "new-access", "refresh_token": "new-refresh"}
        )

    async def sleep(seconds):
        sleeps.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth = CodexOAuthProbe(client, sleep=sleep)
        attempt = await oauth.device()
        assert str(attempt.challenge.verification_uri) == ISSUER + "/codex/device"
        try:
            grant = await attempt.wait()
            refreshed = await oauth.refresh(grant)
            assert refreshed.identity == grant.identity
            assert refreshed.secret_payload["refresh_token"] == "new-refresh"
        finally:
            await attempt.close()
    assert sleeps == [7, 7]
    assert len(requests) == 6


@pytest.mark.parametrize("cancel", [False, True])
async def test_pending_device_poll_has_deadline_and_cancellation(cancel):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def handler(request):
        if request.url.path.endswith("usercode"):
            return httpx.Response(
                200, json={"device_auth_id": "id", "user_code": "code", "interval": "5"}
            )
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth = CodexOAuthProbe(client, authorization_timeout=60 if cancel else 0.1)
        attempt = await oauth.device()
        await entered.wait()
        if cancel:
            await attempt.cancel()
        with pytest.raises(AuthError) as failure:
            await attempt.wait()
        assert failure.value.code == (
            "authorization_cancelled" if cancel else "authorization_expired"
        )
        await attempt.close()
        assert stopped.is_set()


@pytest.mark.parametrize("interval", [0, -1, True, "garbage", "1.5", 901])
async def test_invalid_poll_interval_cannot_start_a_busy_loop(interval):
    def handler(request):
        return httpx.Response(
            200, json={"device_auth_id": "id", "user_code": "code", "interval": interval}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProbeError, match="invalid_poll_interval"):
            await CodexOAuthProbe(client).device()
