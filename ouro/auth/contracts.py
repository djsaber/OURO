from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from threading import Event
from typing import Literal, Protocol, Self

from pydantic import AwareDatetime, ConfigDict, Field, JsonValue, field_validator, model_validator

from ouro.core.base import OuroModel
from ouro.core.errors import CredentialError

CREDENTIAL_ID_PATTERN = r"^cred_[0-9a-f]{32}$"
AUTH_ID_PATTERN = r"^[a-z][a-z0-9-]*$"
CredentialState = Literal["active", "reauth_required", "logged_out"]
AuthErrorCode = Literal[
    "authentication_target_rejected",
    "authentication_required",
    "credential_conflict",
    "credential_store_error",
    "credential_lock_timeout",
    "credential_refresh_failed",
    "auth_closed",
    "authorization_unsupported",
    "authorization_failed",
    "authorization_denied",
    "authorization_expired",
    "authorization_cancelled",
    "authorization_cleanup_failed",
    "authorization_configuration_error",
    "authorization_operation_missing",
    "authorization_operation_limit",
]
_ERROR_MESSAGES: dict[AuthErrorCode, str] = {
    "authentication_target_rejected": "Model request target is not allowed for this credential.",
    "authentication_required": "Credential requires login.",
    "credential_conflict": "Credential changed during this operation.",
    "credential_store_error": "Credential storage is unavailable or invalid.",
    "credential_lock_timeout": "Timed out waiting to commit credential state.",
    "credential_refresh_failed": "Credential refresh did not complete; retry or log in again.",
    "auth_closed": "Credential service is closed.",
    "authorization_unsupported": "The authorization method is not supported.",
    "authorization_failed": "Authorization did not complete; start a new login.",
    "authorization_denied": "Authorization was denied.",
    "authorization_expired": "Authorization expired; start a new login.",
    "authorization_cancelled": "Authorization was cancelled.",
    "authorization_cleanup_failed": "Authorization resources could not be closed cleanly.",
    "authorization_configuration_error": (
        "Provider authentication configuration is unavailable or changed."
    ),
    "authorization_operation_missing": "Authorization operation is unknown or no longer retained.",
    "authorization_operation_limit": (
        "Too many pending authorization operations; cancel one before starting another."
    ),
}


class AuthError(CredentialError):
    """Safe error facts; external payloads must never become the public message."""

    def __init__(self, code: AuthErrorCode) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


class AuthModel(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class CredentialIdentity(AuthModel):
    """Display and routing metadata, never proof of subscription entitlement."""

    display_name: str | None = None
    account_id: str | None = None
    workspace_id: str | None = None


class CredentialGrant(AuthModel):
    """Complete replacement material, decoded and normalized by the owning driver."""

    secret_payload: dict[str, JsonValue] = Field(min_length=1, repr=False)
    expires_at: AwareDatetime | None = None
    identity: CredentialIdentity | None = None


class CredentialRecord(AuthModel):
    format_version: Literal[1] = 1
    credential_id: str = Field(pattern=CREDENTIAL_ID_PATTERN)
    driver_id: str = Field(min_length=1, pattern=AUTH_ID_PATTERN)
    revision: int = Field(ge=1, strict=True)
    state: CredentialState
    expires_at: AwareDatetime | None = None
    identity: CredentialIdentity | None = None
    secret_payload: dict[str, JsonValue] = Field(default_factory=dict, repr=False)

    @field_validator("format_version", mode="before")
    @classmethod
    def require_current_format(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported credential format")
        return value

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.state == "active" and not self.secret_payload:
            raise ValueError("active credential requires secret material")
        if self.state == "logged_out" and (
            self.secret_payload or self.expires_at is not None or self.identity is not None
        ):
            raise ValueError("logged out credential must not retain account material")
        return self


class CredentialSummary(AuthModel):
    """Local facts only; this projection cannot serialize secret material."""

    credential_id: str
    driver_id: str
    revision: int
    state: CredentialState
    expires_at: AwareDatetime | None = None
    identity: CredentialIdentity | None = None

    @classmethod
    def from_record(cls, record: CredentialRecord) -> Self:
        return cls(
            credential_id=record.credential_id,
            driver_id=record.driver_id,
            revision=record.revision,
            state=record.state,
            expires_at=record.expires_at,
            identity=record.identity,
        )


class CredentialStore(Protocol):
    """Atomic snapshots and one lock per credential; service owns all mutations."""

    def read(self, credential_id: str) -> CredentialRecord | None: ...

    def write(self, record: CredentialRecord) -> None:
        """Replace a record while the caller holds its credential lock."""
        ...

    def list_ids(self) -> tuple[str, ...]: ...

    def lock(self, credential_id: str, *, timeout: float) -> AbstractAsyncContextManager[None]: ...


# Drivers provide their bound refresh method. Token shape and rotation rules stay
# outside the service; the grant must include any refresh token retained by a driver.
RefreshCredential = Callable[[CredentialRecord], Awaitable[CredentialGrant]]


class CommitGuard(Protocol):
    """Synchronous binding transaction, entered and released in the save worker.

    Acquisition must honor the remaining timeout and cancellation signal. The
    guard must not await or acquire credential locks, and exit must only release
    its resources; it cannot suppress a save failure or perform another write.
    """

    def __call__(self, *, timeout: float, cancel_event: Event) -> AbstractContextManager[None]: ...
