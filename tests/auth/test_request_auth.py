from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from ouro.auth.contracts import AuthError, CredentialGrant
from ouro.auth.credentials import CredentialService
from ouro.auth.managed_requests import ManagedRequestAuthenticator, RequestTarget
from ouro.auth.request_auth import StaticRequestAuthenticator
from ouro.auth.store import FileCredentialStore
from ouro.core.base import new_id
from tests.auth.test_driver_contracts import NOW, BrowserFixtureDriver, DeviceFixtureDriver

URL = "https://fixture.invalid/api/messages"


async def authenticated(tmp_path, driver=None):
    driver = driver or BrowserFixtureDriver()
    service = CredentialService(FileCredentialStore(tmp_path))
    credential_id = new_id("cred")
    attempt = await driver.begin_authorization(driver.descriptor.methods[0].method_id)
    grant = await attempt.wait()
    await attempt.close()
    record = await service.commit(
        credential_id, driver.descriptor.driver_id, grant, expected_revision=0
    )
    auth = ManagedRequestAuthenticator(
        service,
        credential_id,
        driver,
        service_id="fixture-service",
        targets=(RequestTarget("https://fixture.invalid", "/api"),),
        clock=lambda: NOW,
    )
    return service, record, auth


@pytest.mark.parametrize("driver", [BrowserFixtureDriver(), DeviceFixtureDriver()])
async def test_snapshot_is_immutable_and_fresh_acquisition_observes_logout(tmp_path, driver):
    service, record, auth = await authenticated(tmp_path, driver)
    try:
        snapshot = await auth.acquire(URL)
        assert snapshot.revision == record.revision
        assert snapshot.scope[0] == "fixture-service"
        for secret in snapshot.headers.values():
            assert secret not in repr(snapshot)
        with pytest.raises(TypeError):
            snapshot.headers["x-changed"] = "bad"
        await service.logout(record.credential_id, record.driver_id)
        assert snapshot.headers  # Already authorized attempts retain their own material.
        with pytest.raises(AuthError) as failure:
            await auth.acquire(URL)
        assert failure.value.code == "authentication_required"
    finally:
        await service.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://fixture.invalid/api/messages",
        "https://fixture.invalid.attacker/api/messages",
        "https://fixture.invalid:444/api/messages",
        "https://fixture.invalid:0/api/messages",
        "https://fixture.invalid/api-other/messages",
        "https://fixture.invalid/api/../elsewhere",
        "https://fixture.invalid/api/%2e%2e/elsewhere",
        "https://fixture.invalid/api/%252e%252e/elsewhere",
        "https://user@fixture.invalid/api/messages",
        "https://fixture.invalid/api/messages#fragment",
        "https://fixture.invalid/api\\elsewhere",
        "https://fixture.invalid/api/\nmessages",
        "https://fixture.invalid:bad/api/messages",
    ],
)
async def test_target_rejected_before_any_credential_read(tmp_path, monkeypatch, url):
    service, _, auth = await authenticated(tmp_path)

    async def must_not_read(*args):
        pytest.fail("invalid target must not read secrets")

    monkeypatch.setattr(service, "read", must_not_read)
    try:
        with pytest.raises(AuthError) as failure:
            await auth.acquire(url)
        assert failure.value.code == "authentication_target_rejected"
        assert url not in str(failure.value)
    finally:
        await service.close()


async def test_expiry_and_concurrent_401_share_one_rotation_and_keep_scope(tmp_path):
    class CountingDriver(BrowserFixtureDriver):
        refreshes = 0

        async def refresh(self, record):
            self.refreshes += 1
            await asyncio.sleep(0)
            return await super().refresh(record)

    driver = CountingDriver()
    service, record, auth = await authenticated(tmp_path, driver)
    try:
        original = await auth.acquire(URL)
        assert all(await asyncio.gather(*(auth.recover(original) for _ in range(5))))
        assert driver.refreshes == 1
        renewed = await auth.acquire(URL)
        assert renewed.revision == original.revision + 1
        assert renewed.scope == original.scope
        assert renewed.headers != original.headers
        assert await auth.recover(original)  # A late failure cannot invalidate newer material.
        assert driver.refreshes == 1
        assert (await service.read(record.credential_id)).state == "active"
    finally:
        await service.close()


async def test_expiring_credential_refreshes_before_encoding(tmp_path):
    service, record, auth = await authenticated(tmp_path)
    try:
        await service.commit(
            record.credential_id,
            record.driver_id,
            CredentialGrant(
                secret_payload=record.secret_payload,
                expires_at=NOW + timedelta(seconds=10),
            ),
            expected_revision=record.revision,
        )
        snapshot = await auth.acquire(URL)
        assert snapshot.revision == 3
        assert snapshot.headers["Authorization"] == "Bearer fixture-rotated"
    finally:
        await service.close()


async def test_driver_without_refresh_can_reuse_new_login_but_not_expired_material(tmp_path):
    service, record, auth = await authenticated(tmp_path, DeviceFixtureDriver())
    try:
        original = await auth.acquire(URL)
        assert not await auth.recover(original)
        current = await service.commit(
            record.credential_id,
            record.driver_id,
            CredentialGrant(
                secret_payload={"session": {"ticket": "new-ticket", "workspace": "workspace-c"}},
            ),
            expected_revision=1,
        )
        assert await auth.recover(original)
        latest = await auth.acquire(URL)
        assert latest.revision == 2 and latest.scope != original.scope
        assert latest.headers["X-Fixture-Session"] == "new-ticket"
        await service.commit(
            record.credential_id,
            record.driver_id,
            CredentialGrant(
                secret_payload=current.secret_payload,
                expires_at=NOW,
            ),
            expected_revision=2,
        )
        with pytest.raises(AuthError, match="requires login"):
            await auth.acquire(URL)
    finally:
        await service.close()


@pytest.mark.parametrize(
    "code,state",
    [("authentication_required", "reauth_required"), ("credential_refresh_failed", "active")],
)
async def test_failed_refresh_retains_core_state_semantics(tmp_path, code, state):
    class RejectingDriver(BrowserFixtureDriver):
        async def refresh(self, record):
            raise AuthError(code)

    service, record, auth = await authenticated(tmp_path, RejectingDriver())
    try:
        with pytest.raises(AuthError) as failure:
            await auth.recover(await auth.acquire(URL))
        assert failure.value.code == code
        assert (await service.read(record.credential_id)).state == state
    finally:
        await service.close()


async def test_static_auth_preserves_custom_http_targets_and_copies_material():
    headers = {"authorization": "Bearer static-secret"}
    auth = StaticRequestAuthenticator(headers)
    headers.clear()
    snapshot = await auth.acquire("http://localhost:1234/custom")
    assert snapshot.headers["authorization"] == "Bearer static-secret"
    assert snapshot.revision is None and not await auth.recover(snapshot)


@pytest.mark.parametrize(
    "url",
    [
        "https://fixture.invalid/api",
        "https://fixture.invalid:443/api/messages?mode=stream",
        "https://FIXTURE.invalid/api/messages",
    ],
)
async def test_exact_origin_and_path_subtree_allow_normal_targets(tmp_path, url):
    service, _, auth = await authenticated(tmp_path)
    try:
        assert (await auth.acquire(url)).revision == 1
    finally:
        await service.close()


async def test_malformed_driver_material_never_exposes_its_raw_exception(tmp_path):
    class BrokenDriver(BrowserFixtureDriver):
        def encode(self, record):
            raise ValueError("fixture-access should never reach events")

    service, _, auth = await authenticated(tmp_path, BrokenDriver())
    try:
        with pytest.raises(AuthError) as failure:
            await auth.acquire(URL)
        assert failure.value.code == "authentication_required"
        assert "fixture-access" not in str(failure.value)
    finally:
        await service.close()
