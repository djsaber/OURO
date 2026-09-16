from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ouro.auth.authorization import TaskAuthorizationAttempt
from ouro.auth.contracts import (
    AuthError,
    AuthModel,
    CredentialGrant,
    CredentialIdentity,
    CredentialRecord,
)
from ouro.auth.credentials import CredentialService
from ouro.auth.driver_contracts import (
    AuthDriver,
    AuthDriverDescriptor,
    AuthorizationAttempt,
    AuthorizationMethod,
    BrowserAuthorizationChallenge,
    DeviceAuthorizationChallenge,
    RequestAuthMaterial,
)
from ouro.auth.store import FileCredentialStore
from ouro.core.base import new_id

NOW = datetime(2026, 9, 16, tzinfo=UTC)


class BearerPayload(AuthModel):
    access: str
    refresh: str
    subject: str


class BrowserFixtureDriver:
    descriptor = AuthDriverDescriptor(
        driver_id="fixture-browser",
        label="Browser fixture",
        methods=(
            AuthorizationMethod(
                method_id="external-browser", kind="browser_callback", label="Use browser"
            ),
        ),
    )

    async def begin_authorization(self, method_id: str) -> AuthorizationAttempt:
        if method_id != "external-browser":
            raise AuthError("authorization_unsupported")

        async def authorize() -> CredentialGrant:
            return CredentialGrant(
                secret_payload={
                    "access": "fixture-access",
                    "refresh": "fixture-refresh",
                    "subject": "account-a",
                },
                expires_at=NOW + timedelta(hours=1),
                identity=CredentialIdentity(account_id="account-a"),
            )

        async def cleanup() -> None:
            pass

        return TaskAuthorizationAttempt(
            BrowserAuthorizationChallenge.model_validate(
                {
                    "authorization_url": "https://fixture.invalid/authorize",
                    "expires_at": NOW + timedelta(minutes=1),
                }
            ),
            authorize=authorize,
            cleanup=cleanup,
            clock=lambda: NOW,
        )

    async def refresh(self, record: CredentialRecord) -> CredentialGrant:
        payload = self._payload(record)
        return CredentialGrant(
            secret_payload={
                "access": "fixture-rotated",
                "refresh": payload.refresh,
                "subject": payload.subject,
            },
            expires_at=NOW + timedelta(hours=2),
            identity=record.identity,
        )

    def encode(self, record: CredentialRecord) -> RequestAuthMaterial:
        payload = self._payload(record)
        return RequestAuthMaterial(
            headers={"Authorization": f"Bearer {payload.access}"},
            account_scope=(payload.subject,),
        )

    @staticmethod
    def _payload(record: CredentialRecord) -> BearerPayload:
        try:
            return BearerPayload.model_validate(record.secret_payload)
        except ValidationError:
            raise AuthError("authentication_required") from None


class SessionPayload(AuthModel):
    ticket: str
    workspace: str


class SessionEnvelope(AuthModel):
    session: SessionPayload


class DeviceFixtureDriver:
    descriptor = AuthDriverDescriptor(
        driver_id="fixture-device",
        label="Device fixture",
        methods=(
            AuthorizationMethod(method_id="link-device", kind="device_code", label="Link device"),
        ),
    )
    refresh = None

    async def begin_authorization(self, method_id: str) -> AuthorizationAttempt:
        if method_id != "link-device":
            raise AuthError("authorization_unsupported")

        async def authorize() -> CredentialGrant:
            return CredentialGrant(
                secret_payload={
                    "session": {"ticket": "fixture-ticket", "workspace": "workspace-b"}
                },
                identity=CredentialIdentity(workspace_id="workspace-b"),
            )

        async def cleanup() -> None:
            pass

        return TaskAuthorizationAttempt(
            DeviceAuthorizationChallenge.model_validate(
                {
                    "verification_uri": "https://fixture.invalid/activate",
                    "user_code": "fixture-code",
                    "expires_at": NOW + timedelta(minutes=1),
                }
            ),
            authorize=authorize,
            cleanup=cleanup,
            clock=lambda: NOW,
        )

    def encode(self, record: CredentialRecord) -> RequestAuthMaterial:
        try:
            payload = SessionEnvelope.model_validate(record.secret_payload)
        except ValidationError:
            raise AuthError("authentication_required") from None
        return RequestAuthMaterial(
            headers={"X-Fixture-Session": payload.session.ticket},
            account_scope=(payload.session.workspace,),
        )


def drivers() -> tuple[AuthDriver, ...]:
    # The explicit return type also statically checks both implementations against the port.
    return BrowserFixtureDriver(), DeviceFixtureDriver()


@pytest.mark.parametrize("driver", drivers(), ids=lambda driver: driver.descriptor.driver_id)
async def test_distinct_drivers_authorize_commit_encode_and_logout_through_the_same_core(
    tmp_path: Path, driver: AuthDriver
) -> None:
    store = FileCredentialStore(tmp_path)
    service = CredentialService(store)
    credential_id = new_id("cred")
    method = driver.descriptor.methods[0]
    with pytest.raises(AuthError) as failure:
        await driver.begin_authorization("not-a-menu-index")
    assert failure.value.code == "authorization_unsupported"
    attempt = await driver.begin_authorization(method.method_id)
    try:
        assert attempt.challenge.kind == method.kind
        grant = await attempt.wait()
        # Network authorization owns no persistence; only an explicit conditional commit writes.
        assert not store.directory.exists()
        record = await service.commit(
            credential_id, driver.descriptor.driver_id, grant, expected_revision=0
        )
        material = driver.encode(record)
        assert material.headers
        if driver.refresh is not None:
            updated = await service.refresh(
                credential_id,
                driver.descriptor.driver_id,
                expected_revision=1,
                refresh=driver.refresh,
            )
            replacement = driver.encode(updated)
            assert material.headers != replacement.headers
            assert material.account_scope == replacement.account_scope
        summary = await service.status(credential_id)
        assert summary is not None and summary.state == "active"
        assert "secret_payload" not in summary.model_dump()
        await service.logout(credential_id, driver.descriptor.driver_id)
        cleared = await service.read(credential_id)
        assert cleared is not None and cleared.secret_payload == {}
    finally:
        await attempt.close()
        await service.close()


async def test_different_driver_payloads_are_not_interpreted_by_the_generic_store(
    tmp_path: Path,
) -> None:
    service = CredentialService(FileCredentialStore(tmp_path))
    browser, device = drivers()
    attempt = await browser.begin_authorization("external-browser")
    try:
        record = await service.commit(
            new_id("cred"), browser.descriptor.driver_id, await attempt.wait(), expected_revision=0
        )
        assert browser.encode(record).headers == {"Authorization": "Bearer fixture-access"}
        with pytest.raises(AuthError) as failure:
            device.encode(record)
        assert failure.value.code == "authentication_required"
        assert "fixture-access" not in str(failure.value)
    finally:
        await attempt.close()
        await service.close()
