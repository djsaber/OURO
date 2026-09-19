"""OpenAI token exchange, refresh and owned credential format; no persistence."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import Field, field_validator

from ouro.auth.contracts import AuthError, AuthModel, CredentialGrant, CredentialIdentity
from ouro.auth.drivers.openai_errors import OpenAIOAuthError
from ouro.core.base import utc_now

ISSUER = "https://auth.openai.com"


def _header_text(value: object) -> str:
    if not isinstance(value, str) or not value or any(not 32 < ord(c) < 127 for c in value):
        raise ValueError("expected nonempty visible ASCII text")
    return value


class OpenAITokenPayload(AuthModel):
    format_version: Literal[1] = 1
    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    account_id: str

    @field_validator("format_version", mode="before")
    @classmethod
    def current_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported OpenAI credential format")
        return value

    @field_validator("access_token", "refresh_token", "account_id", mode="before")
    @classmethod
    def valid_header_text(cls, value: object) -> str:
        return _header_text(value)


def read_payload(grant: CredentialGrant) -> OpenAITokenPayload:
    try:
        # Missing versions are invalid too: do not silently migrate credentials.
        if "format_version" not in grant.secret_payload:
            raise ValueError
        payload = OpenAITokenPayload.model_validate(grant.secret_payload)
        if grant.identity is None or grant.identity.account_id != payload.account_id:
            raise ValueError
        return payload
    except ValueError:
        raise AuthError("authentication_required") from None


def _required_text(data: dict[str, Any], key: str) -> str:
    try:
        return _header_text(data.get(key))
    except ValueError:
        raise OpenAIOAuthError("invalid_response") from None


def _claims(token: str) -> dict[str, Any]:
    # Routing metadata from the TLS-protected token response, never proof of entitlement.
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return claims if isinstance(claims, dict) else {}
    except ValueError, UnicodeError:
        return {}


def decode_tokens(
    data: dict[str, Any], now: datetime, previous: CredentialGrant | None = None
) -> CredentialGrant:
    prior = read_payload(previous) if previous is not None else None
    access = _required_text(data, "access_token")
    refresh = _required_text(data, "refresh_token") if "refresh_token" in data else None
    if refresh is None and prior is not None:
        refresh = prior.refresh_token
    if refresh is None:
        raise OpenAIOAuthError("missing_refresh_token")
    account_ids = {prior.account_id} if prior is not None else set()
    for token in (data.get("id_token"), access):
        if token is None:
            continue
        if not isinstance(token, str):
            raise OpenAIOAuthError("invalid_response")
        claims = _claims(token)
        nested = claims.get("https://api.openai.com/auth", {})
        if not isinstance(nested, dict):
            raise OpenAIOAuthError("invalid_response")
        for metadata in (claims, nested):
            if metadata.get("chatgpt_compute_residency") not in (None, "", "no_constraint"):
                raise OpenAIOAuthError("unsupported_residency")
            if "chatgpt_account_id" in metadata:
                account_ids.add(_required_text(metadata, "chatgpt_account_id"))
    if len(account_ids) != 1:
        # No organization guessing or account switching during refresh.
        raise OpenAIOAuthError("ambiguous_account_route")
    expires_at = None
    lifetime = data.get("expires_in")
    if lifetime is not None:
        if type(lifetime) is not int or not 0 < lifetime <= 31_536_000:
            raise OpenAIOAuthError("invalid_token_expiry")
        expires_at = now + timedelta(seconds=lifetime)
    payload = OpenAITokenPayload(
        access_token=access, refresh_token=refresh, account_id=account_ids.pop()
    )
    return CredentialGrant(
        secret_payload=payload.model_dump(mode="json"),
        identity=CredentialIdentity(account_id=payload.account_id),
        expires_at=expires_at,
    )


class OpenAITokenClient:
    """Fixed issuer, no redirects/retries. Borrow an injected client or own each request."""

    def __init__(
        self,
        *,
        client_id: str,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.client_id = _header_text(client_id)
        self._client = client
        self._clock = clock

    async def exchange(self, code: str, verifier: str, redirect: str) -> CredentialGrant:
        data = await self._post(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect,
            }
        )
        return decode_tokens(data, self._clock())

    async def refresh(self, grant: CredentialGrant) -> CredentialGrant:
        payload = read_payload(grant)
        try:
            data = await self._post(
                {"grant_type": "refresh_token", "refresh_token": payload.refresh_token}
            )
            return decode_tokens(data, self._clock(), grant)
        except OpenAIOAuthError as failure:
            code = failure.code
            if code != "authentication_required":
                code = "credential_refresh_failed"
            raise OpenAIOAuthError(failure.reason, code=code, status=failure.status) from None

    async def _post(self, data: dict[str, str]) -> dict[str, Any]:
        form = {**data, "client_id": self.client_id}
        try:
            async with asyncio.timeout(30):
                if self._client is not None:
                    response = await self._client.post(
                        ISSUER + "/oauth/token", data=form, timeout=30, follow_redirects=False
                    )
                else:
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            ISSUER + "/oauth/token", data=form, timeout=30, follow_redirects=False
                        )
        except httpx.RequestError, TimeoutError:
            # Unknown exchange outcomes must not trigger automatic token replay.
            raise OpenAIOAuthError("auth_network_error") from None
        if response.status_code != 200:
            terminal = data["grant_type"] == "refresh_token" and _refresh_rejected(response)
            raise OpenAIOAuthError(
                "auth_http_error",
                code="authentication_required" if terminal else "authorization_failed",
                status=response.status_code,
            )
        try:
            result = response.json()
        except ValueError:
            raise OpenAIOAuthError("invalid_response") from None
        if not isinstance(result, dict):
            raise OpenAIOAuthError("invalid_response")
        return result


def _refresh_rejected(response: httpx.Response) -> bool:
    if response.status_code == 401:
        return True
    try:
        data = response.json()
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    error = data.get("error")
    code = error.get("code") if isinstance(error, dict) else error
    if not isinstance(code, str):
        code = data.get("code")
    if not isinstance(code, str):
        return False
    code = code.lower()
    return (response.status_code == 400 and code == "invalid_grant") or code in {
        "refresh_token_expired",
        "refresh_token_reused",
        "refresh_token_invalidated",
    }
