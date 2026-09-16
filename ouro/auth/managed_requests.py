from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import SplitResult, urlsplit

from ouro.auth.contracts import AuthError, CredentialRecord
from ouro.auth.credentials import CredentialService
from ouro.auth.driver_contracts import AuthDriver
from ouro.auth.request_auth import RequestAuthSnapshot
from ouro.core.base import utc_now


def _target(url: str) -> SplitResult:
    # Reject ambiguous paths before a client/proxy can normalize them differently.
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise ValueError("invalid authentication target")
    target = urlsplit(url)
    if (
        target.scheme != "https"
        or not target.hostname
        or target.username is not None
        or target.password is not None
        or target.fragment
        or "%" in target.path
        or any(part in {".", ".."} for part in target.path.split("/"))
    ):
        raise ValueError("invalid authentication target")
    _ = target.port  # Reject malformed ports before touching credentials.
    return target


@dataclass(frozen=True)
class RequestTarget:
    """Trusted composition data: an exact HTTPS origin and a path subtree."""

    origin: str
    path_prefix: str

    def __post_init__(self) -> None:
        origin = _target(self.origin)
        path = _target(self.origin.rstrip("/") + self.path_prefix)
        if (
            origin.path not in {"", "/"}
            or origin.query
            or not self.path_prefix.startswith("/")
            or path.path != self.path_prefix
            or path.query
            or path.fragment
        ):
            raise ValueError("invalid authentication target rule")

    def allows(self, target: SplitResult) -> bool:
        origin = urlsplit(self.origin)
        prefix = self.path_prefix.rstrip("/")
        return (
            target.hostname == origin.hostname
            and (443 if target.port is None else target.port)
            == (443 if origin.port is None else origin.port)
            and (target.path == prefix or target.path.startswith(prefix + "/"))
        )


class ManagedRequestAuthenticator:
    """Borrow the home-scoped credential service; never cache tokens or own its lifetime."""

    def __init__(
        self,
        credentials: CredentialService,
        credential_id: str,
        driver: AuthDriver,
        *,
        service_id: str,
        targets: tuple[RequestTarget, ...],
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not service_id or not targets:
            raise ValueError("managed authentication requires a service and allowed targets")
        self._credentials = credentials
        self._credential_id = credential_id
        self._driver = driver
        self._service_id = service_id
        self._targets = targets
        self._clock = clock

    async def acquire(self, url: str) -> RequestAuthSnapshot:
        try:
            target = _target(url)
            allowed = any(rule.allows(target) for rule in self._targets)
        except ValueError:
            allowed = False
        if not allowed:
            raise AuthError("authentication_target_rejected")
        record = self._active(await self._credentials.read(self._credential_id))
        if (
            record.expires_at is not None
            and (record.expires_at - self._clock()).total_seconds()
            <= self._driver.descriptor.refresh_leeway_seconds
            and self._driver.refresh is not None
        ):
            record = await self._credentials.refresh(
                self._credential_id,
                self._driver.descriptor.driver_id,
                expected_revision=record.revision,
                refresh=self._driver.refresh,
            )
        self._require_unexpired(record)
        try:
            material = self._driver.encode(record)
        except AuthError:
            raise
        except Exception:
            raise AuthError("authentication_required") from None
        return RequestAuthSnapshot(
            headers=material.headers,
            revision=record.revision,
            scope=(self._service_id, *material.account_scope),
        )

    async def recover(self, snapshot: RequestAuthSnapshot) -> bool:
        if snapshot.revision is None:
            return False
        record = self._active(await self._credentials.read(self._credential_id))
        if record.revision < snapshot.revision:
            raise AuthError("credential_conflict")
        if record.revision == snapshot.revision:
            if self._driver.refresh is None:
                return False
            record = await self._credentials.refresh(
                self._credential_id,
                self._driver.descriptor.driver_id,
                expected_revision=snapshot.revision,
                refresh=self._driver.refresh,
            )
        self._require_unexpired(record)
        return True

    def _active(self, record: CredentialRecord | None) -> CredentialRecord:
        if record is None or record.state != "active":
            raise AuthError("authentication_required")
        if record.driver_id != self._driver.descriptor.driver_id:
            raise AuthError("credential_conflict")
        return record

    def _require_unexpired(self, record: CredentialRecord) -> None:
        if record.expires_at is not None and record.expires_at <= self._clock():
            raise AuthError("authentication_required")
