from __future__ import annotations

from typing import Literal

from pydantic import Field

from ouro.auth.contracts import AuthErrorCode, AuthModel, CredentialSummary
from ouro.auth.driver_contracts import AuthorizationChallenge

OperationState = Literal["pending", "succeeded", "failed", "cancelled", "expired"]
ProviderAuthState = Literal[
    "static", "missing", "ready", "refresh_needed", "reauth_required", "logged_out", "unavailable"
]


class LoginOperation(AuthModel):
    operation_id: str
    provider_id: str
    state: OperationState
    error_code: AuthErrorCode | None = None
    message: str | None = None
    credential: CredentialSummary | None = None


class LoginChallenge(AuthModel):
    operation_id: str
    challenge: AuthorizationChallenge = Field(repr=False)


class ProviderAuthStatus(AuthModel):
    provider_id: str
    driver_id: str
    credential_id: str | None
    state: ProviderAuthState
    credential: CredentialSummary | None = None
    error_code: AuthErrorCode | None = None
    bound_provider_ids: tuple[str, ...] = ()


class CredentialView(AuthModel):
    credential: CredentialSummary
    bound_provider_ids: tuple[str, ...]
