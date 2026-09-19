"""Memory-only OAuth experiment against the pinned Codex client contract."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import AnyUrl

from ouro.auth.authorization import TaskAuthorizationAttempt
from ouro.auth.contracts import AuthError, CredentialGrant
from ouro.auth.driver_contracts import (
    DeviceAuthorizationChallenge,
)
from ouro.auth.drivers.openai_codex import OpenAICodexDriver
from ouro.auth.drivers.openai_tokens import ISSUER, OpenAITokenClient
from ouro.core.base import utc_now

CODEX_REVISION = "04581f96044a9fe6062593c4d557036477aee961"
OPENCODE_REVISION = "e03db9bc6908f75c9334d8aa997deeaac81c0298"
# Public upstream client, for an opt-in compatibility experiment only.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class ProbeError(RuntimeError):
    """Only locally chosen codes/statuses may cross the report boundary."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class ProbeAuthorizationError(AuthError):
    """Carry already sanitized experiment facts through the shared attempt boundary."""

    def __init__(self, failure: ProbeError) -> None:
        super().__init__("authorization_failed")
        self.probe_code = failure.code
        self.http_status = failure.status


async def _authorize_with_diagnostics(
    authorize: Callable[[], Awaitable[CredentialGrant]],
) -> CredentialGrant:
    try:
        return await authorize()
    except ProbeError as failure:
        raise ProbeAuthorizationError(failure) from None


def required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ProbeError("invalid_response")
    return value


class CodexOAuthProbe:
    """Borrow an HTTP client; never read caches, write tokens, or register a driver."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        authorization_timeout: float = 900,
    ) -> None:
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.authorization_timeout = authorization_timeout
        self._tokens = OpenAITokenClient(client_id=CLIENT_ID, client=client, clock=clock)
        self._driver = OpenAICodexDriver(
            client_id=CLIENT_ID,
            client=client,
            clock=clock,
            authorization_timeout=authorization_timeout,
            originator="ouro-auth-probe",
        )

    async def _post(self, path: str, **data: str) -> httpx.Response:
        try:
            return await self.client.post(
                ISSUER + path, json=data, timeout=30, follow_redirects=False
            )
        except httpx.RequestError:
            # A token exchange with an unknown outcome must not be retried here.
            raise ProbeError("auth_network_error") from None

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        if response.status_code != 200:
            raise ProbeError("auth_http_error", status=response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise ProbeError("invalid_response") from None
        if not isinstance(data, dict):
            raise ProbeError("invalid_response")
        return data

    async def refresh(self, grant: CredentialGrant) -> CredentialGrant:
        return await self._tokens.refresh(grant)

    async def browser(self) -> TaskAuthorizationAttempt:
        return await self._driver.begin_authorization("browser")

    async def device(self) -> TaskAuthorizationAttempt:
        data = self._json(
            await self._post("/api/accounts/deviceauth/usercode", client_id=CLIENT_ID)
        )
        device_id = required_text(data, "device_auth_id")
        user_code = required_text(data, "user_code" if "user_code" in data else "usercode")
        interval_value = data.get("interval", "5")
        try:
            if type(interval_value) not in (str, int):
                raise ValueError
            interval = int(interval_value)
            if not 1 <= interval <= 900:
                raise ValueError
        except ValueError:
            raise ProbeError("invalid_poll_interval") from None

        async def authorize() -> CredentialGrant:
            while True:
                response = await self._post(
                    "/api/accounts/deviceauth/token", device_auth_id=device_id, user_code=user_code
                )
                if response.status_code in {403, 404}:
                    await self.sleep(interval)
                    continue
                result = self._json(response)
                code = required_text(result, "authorization_code")
                verifier = required_text(result, "code_verifier")
                challenge = required_text(result, "code_challenge")
                expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                if not secrets.compare_digest(challenge, expected.decode().rstrip("=")):
                    raise ProbeError("invalid_pkce_material")
                return await self._tokens.exchange(code, verifier, ISSUER + "/deviceauth/callback")

        async def cleanup() -> None:
            pass  # HTTP requests are scoped by the borrowed client's operations.

        return TaskAuthorizationAttempt(
            DeviceAuthorizationChallenge(
                verification_uri=AnyUrl(ISSUER + "/codex/device"),
                user_code=user_code,
                expires_at=self.clock() + timedelta(seconds=self.authorization_timeout),
            ),
            authorize=lambda: _authorize_with_diagnostics(authorize),
            cleanup=cleanup,
            clock=self.clock,
        )
