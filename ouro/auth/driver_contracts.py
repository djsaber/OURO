from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Annotated, Literal, Protocol, Self

from pydantic import AnyUrl, AwareDatetime, Field, UrlConstraints, model_validator

from ouro.auth.contracts import (
    AUTH_ID_PATTERN,
    AuthModel,
    CredentialGrant,
    CredentialRecord,
    RefreshCredential,
)

AuthorizationKind = Literal["browser_callback", "device_code"]
AuthorizationURL = Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"], host_required=True)]


class AuthorizationMethod(AuthModel):
    method_id: str = Field(pattern=AUTH_ID_PATTERN)
    kind: AuthorizationKind
    label: str = Field(min_length=1)
    description: str | None = None


class AuthDriverDescriptor(AuthModel):
    driver_id: str = Field(pattern=AUTH_ID_PATTERN)
    label: str = Field(min_length=1)
    methods: tuple[AuthorizationMethod, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_methods(self) -> Self:
        if self.driver_id == "static":
            raise ValueError("static authentication is not a managed driver")
        if len({method.method_id for method in self.methods}) != len(self.methods):
            raise ValueError("authorization method ids must be unique within a driver")
        return self


class BrowserAuthorizationChallenge(AuthModel):
    kind: Literal["browser_callback"] = "browser_callback"
    authorization_url: AuthorizationURL = Field(repr=False)
    expires_at: AwareDatetime
    instructions: str | None = None


class DeviceAuthorizationChallenge(AuthModel):
    kind: Literal["device_code"] = "device_code"
    verification_uri: AuthorizationURL = Field(repr=False)
    user_code: str = Field(min_length=1, repr=False)
    verification_uri_complete: AuthorizationURL | None = Field(default=None, repr=False)
    expires_at: AwareDatetime
    instructions: str | None = None


AuthorizationChallenge = Annotated[
    BrowserAuthorizationChallenge | DeviceAuthorizationChallenge, Field(discriminator="kind")
]


class AuthorizationAttempt(Protocol):
    """One driver-owned exchange; its grant is not a committed login result."""

    @property
    def challenge(self) -> AuthorizationChallenge: ...

    async def wait(self) -> CredentialGrant:
        """Wait for material and cleanup. Cancelling a waiter leaves the attempt running."""
        ...

    async def cancel(self) -> None:
        """Stop the exchange and await resource cleanup. Safe to repeat."""
        ...

    async def close(self) -> None:
        """Cancel unfinished authorization and finish cleanup without discarding a ready grant."""
        ...


@dataclass(frozen=True)
class RequestAuthMaterial:
    """Internal driver output, not a public DTO or permission to send to any URL."""

    headers: Mapping[str, str] = field(repr=False)
    account_scope: tuple[str, ...]

    def __post_init__(self) -> None:
        # Freeze a copy so a driver cannot mutate a previous attempt's headers.
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


class AuthDriver(Protocol):
    """Managed authentication only; drivers own protocol I/O, never config or persistence."""

    @property
    def descriptor(self) -> AuthDriverDescriptor: ...

    @property
    def refresh(self) -> RefreshCredential | None:
        """A bound refresh function, or None when this driver cannot refresh."""
        ...

    async def begin_authorization(self, method_id: str) -> AuthorizationAttempt:
        """Reject unknown methods and clean up any resources if beginning fails or is cancelled."""
        ...

    def encode(self, record: CredentialRecord) -> RequestAuthMaterial:
        """Decode owned material and derive stable account/workspace scope, never entitlement."""
        ...
