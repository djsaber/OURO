"""Browser AuthDriver for the verified Codex protocol; not a product registration."""

from __future__ import annotations

import base64
import hashlib
import math
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from pydantic import AnyUrl

from ouro.auth.authorization import TaskAuthorizationAttempt
from ouro.auth.contracts import AuthError, CredentialGrant, CredentialRecord
from ouro.auth.driver_contracts import (
    AuthDriverDescriptor,
    AuthorizationMethod,
    BrowserAuthorizationChallenge,
    RequestAuthMaterial,
)
from ouro.auth.drivers.openai_callback import CallbackReceiver
from ouro.auth.drivers.openai_tokens import ISSUER, OpenAITokenClient, read_payload
from ouro.core.base import utc_now

SCOPES = "openid profile email offline_access"


class OpenAICodexDriver:
    descriptor = AuthDriverDescriptor(
        driver_id="openai-codex",
        label="OpenAI Codex",
        methods=(
            AuthorizationMethod(method_id="browser", kind="browser_callback", label="Browser"),
        ),
    )

    def __init__(
        self,
        *,
        client_id: str,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        authorization_timeout: float = 900,
        originator: str = "ouro",
    ) -> None:
        # Client selection is a trusted composition decision, never an inferred entitlement.
        if not math.isfinite(authorization_timeout) or authorization_timeout <= 0:
            raise ValueError("authorization timeout must be positive and finite")
        if not originator or any(not 32 < ord(c) < 127 for c in originator):
            raise ValueError("invalid originator")
        self._tokens = OpenAITokenClient(client_id=client_id, client=client, clock=clock)
        self._clock = clock
        self._timeout = authorization_timeout
        self._originator = originator

    async def begin_authorization(self, method_id: str) -> TaskAuthorizationAttempt:
        if method_id != "browser":
            raise AuthError("authorization_unsupported")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        receiver = CallbackReceiver(state)
        try:
            await receiver.start(1455)
            redirect = "http://localhost:1455/auth/callback"
            digest = hashlib.sha256(verifier.encode("ascii")).digest()
            query = urlencode(
                {
                    "response_type": "code",
                    "client_id": self._tokens.client_id,
                    "redirect_uri": redirect,
                    "scope": SCOPES,
                    "code_challenge": base64.urlsafe_b64encode(digest).decode().rstrip("="),
                    "code_challenge_method": "S256",
                    "state": state,
                    "id_token_add_organizations": "true",
                    "codex_cli_simplified_flow": "true",
                    "originator": self._originator,
                }
            )

            async def authorize() -> CredentialGrant:
                return await self._tokens.exchange(await receiver.code, verifier, redirect)

            return TaskAuthorizationAttempt(
                BrowserAuthorizationChallenge(
                    authorization_url=AnyUrl(ISSUER + "/oauth/authorize?" + query),
                    expires_at=self._clock() + timedelta(seconds=self._timeout),
                ),
                authorize=authorize,
                cleanup=receiver.close,
                clock=self._clock,
            )
        except BaseException:
            await receiver.close()
            raise

    async def refresh(self, record: CredentialRecord) -> CredentialGrant:
        return await self._tokens.refresh(self._grant(record))

    def encode(self, record: CredentialRecord) -> RequestAuthMaterial:
        payload = read_payload(self._grant(record))
        return RequestAuthMaterial(
            headers={
                "Authorization": "Bearer " + payload.access_token,
                "ChatGPT-Account-Id": payload.account_id,
            },
            account_scope=(payload.account_id,),
        )

    def _grant(self, record: CredentialRecord) -> CredentialGrant:
        if record.driver_id != self.descriptor.driver_id:
            raise AuthError("credential_conflict")
        if record.state != "active":
            raise AuthError("authentication_required")
        return CredentialGrant(
            secret_payload=record.secret_payload,
            identity=record.identity,
            expires_at=record.expires_at,
        )
