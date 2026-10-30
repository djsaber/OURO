from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta

import pytest

import ouro.local_locks as local_locks
from ouro.application.provider_auth.service import ProviderAuthService
from ouro.auth.authorization import TaskAuthorizationAttempt
from ouro.auth.contracts import AuthError, CredentialGrant
from ouro.auth.credentials import CredentialService
from ouro.auth.driver_contracts import BrowserAuthorizationChallenge
from ouro.auth.store import FileCredentialStore
from ouro.config import provider_models
from ouro.config.file import JsonConfigFile
from ouro.config.provider_models import ProviderConnectionSpec, ProviderTypeSpec
from ouro.config.providers.manager import ProviderConfigManager
from tests.auth.test_driver_contracts import NOW, BrowserFixtureDriver, drivers
from tests.concurrency import wait_until


class ControlledDriver(BrowserFixtureDriver):
    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cleaned = 0
        self.error = None
        self.cleanup_error = False

    async def begin_authorization(self, method_id):
        async def authorize():
            self.started.set()
            await self.release.wait()
            if self.error is not None:
                raise self.error
            return CredentialGrant(secret_payload={"access": "fixture-new-secret"})

        async def cleanup():
            self.cleaned += 1
            if self.cleanup_error:
                raise OSError("fixture-secret-cleanup-error")

        return TaskAuthorizationAttempt(
            BrowserAuthorizationChallenge.model_validate(
                {
                    "authorization_url": "https://fixture.invalid/authorize?code=fixture-private-code",
                    "expires_at": NOW + timedelta(minutes=1),
                }
            ),
            authorize=authorize,
            cleanup=cleanup,
            clock=lambda: NOW,
        )


@pytest.fixture
async def auth_home(tmp_path, monkeypatch, request):
    spec = ProviderTypeSpec(
        "custom",
        ProviderConnectionSpec("openai-chat-completions"),
        (
            ProviderConnectionSpec("openai-chat-completions", "fixture-browser", "service-browser"),
            ProviderConnectionSpec("anthropic-messages", "fixture-device", "service-device"),
        ),
    )
    monkeypatch.setitem(provider_models._PROVIDER_TYPE_SPECS, spec.provider_type, spec)
    config = ProviderConfigManager(tmp_path)
    store = FileCredentialStore(tmp_path)
    credentials = CredentialService(store, **getattr(request, "param", {}))
    services = []

    def create(driver=None, **options):
        driver = driver or ControlledDriver()
        driver_id = driver.descriptor.driver_id
        protocol = (
            "anthropic-messages" if driver_id == "fixture-device" else "openai-chat-completions"
        )
        if not config.snapshot(include_builtin=False).providers:
            config.create_provider(
                display_name="Subscription",
                provider_type="custom",
                protocol=protocol,
                auth_driver=driver_id,
                base_url="https://fixture.invalid/v1",
            )
        service = ProviderAuthService(config, credentials, (driver,), clock=lambda: NOW, **options)
        services.append(service)
        return config, store, credentials, service, driver

    yield create
    for service in services:
        await service.close()
    await credentials.close()


async def begin(service, driver):
    return await service.begin(_provider_id(service), driver.descriptor.methods[0].method_id)


def _provider_id(owner) -> str:
    config = owner if isinstance(owner, ProviderConfigManager) else owner._config
    providers = config.snapshot(include_builtin=False).providers
    assert len(providers) == 1
    return providers[0].provider_id


@pytest.mark.parametrize("driver", drivers(), ids=lambda driver: driver.descriptor.driver_id)
async def test_two_existing_drivers_share_login_status_forget_and_logout(auth_home, driver):
    config, store, credentials, service, driver = auth_home(driver)
    provider_id = _provider_id(config)
    assert (await service.status(provider_id)).state == "missing"
    assert not store.directory.exists()
    assert await service.methods(provider_id) == driver.descriptor.methods
    started = await begin(service, driver)
    assert started.challenge.kind == driver.descriptor.methods[0].kind
    result = await service.wait(started.operation_id)
    assert result.state == "succeeded" and result.credential.revision == 1
    assert await service.cancel(started.operation_id) == result
    assert await service.wait(started.operation_id) == result
    status = await service.status(provider_id)
    assert status.state == "ready"
    assert status.bound_provider_ids == (provider_id,)
    material = driver.encode(await credentials.read(status.credential_id))
    assert material.headers
    listed = await service.credentials()
    assert listed[0].credential == status.credential
    assert listed[0].bound_provider_ids == (provider_id,)
    erased = await service.forget(status.credential_id)
    assert erased.bound_provider_ids == (provider_id,)
    assert erased.credential.state == "logged_out"
    assert await service.logout(provider_id) == erased
    assert (await service.status(provider_id)).state == "logged_out"
    for dto in (result, status, listed[0], erased):
        serialized = dto.model_dump_json()
        assert "secret_payload" not in serialized
        assert "fixture-access" not in serialized and "fixture-ticket" not in serialized
        assert "verification_uri" not in serialized and "authorization_url" not in serialized
        assert "user_code" not in serialized


async def test_static_missing_provider_unknown_method_and_unknown_driver(auth_home):
    config, _, credentials, service, driver = auth_home()
    provider_id = _provider_id(config)
    assert await service.methods("faux") == ()
    assert (await service.status("faux")).state == "static"
    for provider, method in (("faux", "external-browser"), (provider_id, "unknown")):
        with pytest.raises(AuthError) as error:
            await service.begin(provider, method)
        assert error.value.code == "authorization_unsupported"
    with pytest.raises(AuthError) as error:
        await service.methods("missing")
    assert error.value.code == "authorization_configuration_error"
    empty = ProviderAuthService(config, credentials, ())
    assert (await empty.status(provider_id)).state == "unavailable"
    with pytest.raises(AuthError):
        await begin(empty, driver)
    await empty.close()


@pytest.mark.parametrize(
    "failure,state,code",
    [
        (AuthError("authorization_denied"), "failed", "authorization_denied"),
        (AuthError("authorization_expired"), "expired", "authorization_expired"),
        (OSError("fixture-secret-from-response"), "failed", "authorization_failed"),
        (None, "cancelled", "authorization_cancelled"),
    ],
)
async def test_failed_or_cancelled_relogin_keeps_previous_record(auth_home, failure, state, code):
    config, store, credentials, service, driver = auth_home()
    provider = config.get_provider(_provider_id(config))
    assert provider is not None
    credential_id = provider.credential_ref.name
    original = await credentials.commit(
        credential_id,
        driver.descriptor.driver_id,
        CredentialGrant(secret_payload={"old": "fixture-old-secret"}),
        expected_revision=0,
    )
    driver.error = failure
    started = await begin(service, driver)
    pending = service.operation(started.operation_id)
    assert pending.state == "pending"
    assert "fixture-private-code" not in pending.model_dump_json()
    if failure is None:
        result = await service.cancel(started.operation_id)
    else:
        driver.release.set()
        result = await service.wait(started.operation_id)
    assert (result.state, result.error_code) == (state, code)
    assert "fixture-secret" not in result.model_dump_json()
    assert store.read(credential_id) == original
    assert driver.cleaned == 1
    assert await service.cancel(started.operation_id) == result


async def test_waiter_cancellation_does_not_cancel_login_or_second_waiter(auth_home):
    _, _, _, service, driver = auth_home()
    started = await begin(service, driver)
    waiter = asyncio.create_task(service.wait(started.operation_id))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert service.operation(started.operation_id).state == "pending"
    driver.release.set()
    assert (await service.wait(started.operation_id)).state == "succeeded"


@pytest.mark.parametrize(
    "change", ["remove", "replace", "base_url", "endpoint_target", "logout", "refresh"]
)
async def test_binding_or_revision_change_rejects_old_login(auth_home, change):
    config, store, credentials, service, driver = auth_home()
    provider_id = _provider_id(config)
    provider = config.get_provider(provider_id)
    assert provider is not None
    credential_id = provider.credential_ref.name
    if change == "refresh":
        await credentials.commit(
            credential_id,
            driver.descriptor.driver_id,
            CredentialGrant(secret_payload={"old": "fixture-old-secret"}),
            expected_revision=0,
        )
    started = await begin(service, driver)
    if change == "remove":
        config.remove_provider(provider_id)
    elif change == "replace":
        config.replace_provider(
            provider_id,
            display_name="Subscription",
            provider_type="custom",
            protocol="openai-chat-completions",
            auth_driver=driver.descriptor.driver_id,
            base_url="https://fixture.invalid/v1",
        )
    elif change == "base_url":
        config.update_provider(provider_id, base_url="https://other.invalid/v1")
    elif change == "endpoint_target":
        config.create_endpoint(provider_id, model_id="fixture", base_url="https://other.invalid/v1")
    elif change == "logout":
        await service.logout(provider_id)
    else:

        async def refresh(record):
            return CredentialGrant(secret_payload={"new": "fixture-refreshed-secret"})

        await credentials.refresh(
            credential_id, driver.descriptor.driver_id, expected_revision=1, refresh=refresh
        )
    current = store.read(credential_id)
    driver.release.set()
    result = await service.wait(started.operation_id)
    assert result.state == "failed" and result.error_code == "credential_conflict"
    assert store.read(credential_id) == current


async def test_unrelated_configuration_change_does_not_invalidate_login(auth_home):
    config, _, _, service, driver = auth_home()
    started = await begin(service, driver)
    provider_id = _provider_id(config)
    config.update_provider(provider_id, timeout_seconds=42)
    config.create_endpoint(provider_id, model_id="fixture", make_default=True)
    driver.release.set()
    assert (await service.wait(started.operation_id)).state == "succeeded"


async def test_parallel_logins_cannot_both_replace_same_revision(auth_home):
    _, _, _, service, driver = auth_home()
    first, second = await begin(service, driver), await begin(service, driver)
    driver.release.set()
    results = await asyncio.gather(
        service.wait(first.operation_id), service.wait(second.operation_id)
    )
    assert sorted(result.state for result in results) == ["failed", "succeeded"]
    assert (
        next(result for result in results if result.state == "failed").error_code
        == "credential_conflict"
    )


@contextmanager
def held_config(config):
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with config.mutation():
            acquired.set()
            assert release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert acquired.wait(5)
    try:
        yield release
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "action,auth_home",
    [("cancel", {}), ("timeout", {"lock_timeout": 0.25}), ("close", {})],
    indirect=["auth_home"],
    ids=["cancel", "timeout", "close"],
)
async def test_config_lock_wait_is_bounded_and_never_commits_late(auth_home, monkeypatch, action):
    config, store, credentials, service, driver = auth_home()
    provider = config.get_provider(_provider_id(config))
    assert provider is not None
    credential_id = provider.credential_ref.name
    if action == "timeout":
        # Spend the short budget on the contended configuration lock, not worker scheduling.
        monkeypatch.setattr("ouro.auth.credentials.monotonic", lambda: 100.0)
        monkeypatch.setattr("ouro.auth.commit.monotonic", lambda: 100.0)
    started = await begin(service, driver)
    entered = threading.Event()
    mutation = config.mutation

    @contextmanager
    def observed_mutation(**kwargs):
        entered.set()
        with mutation(**kwargs):
            yield

    with held_config(config):
        monkeypatch.setattr(config, "mutation", observed_mutation)
        driver.release.set()
        assert await asyncio.to_thread(entered.wait, 5)
        if action == "cancel":
            result = await asyncio.wait_for(service.cancel(started.operation_id), 2)
            assert result.state == "cancelled"
        elif action == "close":
            await asyncio.wait_for(service.close(), 2)
            assert service.operation(started.operation_id).state == "cancelled"
        else:
            result = await asyncio.wait_for(service.wait(started.operation_id), 2)
            assert result.state == "failed" and result.error_code == "credential_lock_timeout"
        assert store.read(credential_id) is None
    # Lock release cannot wake an abandoned transaction after cancellation returned.
    async with store.lock(credential_id, timeout=0.1):
        assert store.read(credential_id) is None


@pytest.mark.parametrize("fail_save", [False, True])
async def test_save_wins_cancellation_and_config_changes_wait_for_result(
    auth_home, monkeypatch, fail_save
):
    config, store, _, service, driver = auth_home()
    entered, release, config_changed = threading.Event(), threading.Event(), threading.Event()
    original_write = store.write
    contended = threading.Event()
    acquire = local_locks.FileMutationLock.acquire

    def observed_acquire(lock, **kwargs):
        if not lock._local_lock.acquire(blocking=False):
            contended.set()
        else:
            lock._local_lock.release()
        return acquire(lock, **kwargs)

    monkeypatch.setattr(local_locks.FileMutationLock, "acquire", observed_acquire)

    def write(record):
        entered.set()
        release.wait()
        if fail_save:
            raise AuthError("credential_store_error")
        original_write(record)

    monkeypatch.setattr(store, "write", write)
    started = await begin(service, driver)
    driver.release.set()

    def change_config():
        config.update_provider(_provider_id(config), base_url="https://changed.invalid/v1")
        config_changed.set()

    tasks = []
    try:
        assert await asyncio.to_thread(entered.wait, 30)
        assert service.operation(started.operation_id).state == "pending"
        cancelled = asyncio.create_task(service.cancel(started.operation_id))
        mutation = asyncio.create_task(asyncio.to_thread(change_config))
        tasks.extend((cancelled, mutation))
        await wait_until(
            lambda: service._operations[started.operation_id].task.cancelling(),
            description="cancellation reaching the active login",
        )
        assert await asyncio.to_thread(contended.wait, 30)
        assert not cancelled.done() and not config_changed.is_set()
        cancelled.cancel()
        release.set()
        result = await cancelled
        await mutation
        assert result.state == ("failed" if fail_save else "succeeded")
        assert result.error_code == ("credential_store_error" if fail_save else None)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cleanup_failure_is_not_a_successful_login(auth_home):
    config, store, _, service, driver = auth_home()
    driver.cleanup_error = True
    started = await begin(service, driver)
    driver.release.set()
    result = await service.wait(started.operation_id)
    assert result.state == "failed" and result.error_code == "authorization_cleanup_failed"
    assert "fixture-secret" not in result.model_dump_json()
    provider = config.get_provider(_provider_id(config))
    assert provider is not None
    assert store.read(provider.credential_ref.name) is None


async def test_bounded_retention_never_discards_pending_operations(auth_home):
    _, _, _, service, driver = auth_home(operation_limit=1)
    first = await begin(service, driver)
    with pytest.raises(AuthError) as error:
        await begin(service, driver)
    assert error.value.code == "authorization_operation_limit"
    await service.cancel(first.operation_id)
    second = await begin(service, driver)
    with pytest.raises(AuthError) as error:
        service.operation(first.operation_id)
    assert error.value.code == "authorization_operation_missing"
    assert service.operation(second.operation_id).state == "pending"
    await service.close()
    assert service.operation(second.operation_id).state == "cancelled"
    with pytest.raises(AuthError) as error:
        await begin(service, driver)
    assert error.value.code == "auth_closed"


@pytest.mark.parametrize("can_refresh", [False, True])
async def test_expiry_status_does_not_execute_network(auth_home, can_refresh):
    driver = drivers()[0 if can_refresh else 1]
    config, _, credentials, service, _ = auth_home(driver)
    provider = config.get_provider(_provider_id(config))
    assert provider is not None
    credential_id = provider.credential_ref.name
    await credentials.commit(
        credential_id,
        driver.descriptor.driver_id,
        CredentialGrant(
            secret_payload={"fixture": "expired"}, expires_at=NOW - timedelta(seconds=1)
        ),
        expected_revision=0,
    )
    status = await service.status(_provider_id(config))
    assert status.state == ("refresh_needed" if can_refresh else "reauth_required")
    assert status.credential.revision == 1


async def test_corrupt_record_is_safe_status_and_does_not_break_construction(auth_home):
    config, store, credentials, service, driver = auth_home()
    provider = config.get_provider(_provider_id(config))
    assert provider is not None
    credential_id = provider.credential_ref.name
    await credentials.logout(credential_id, driver.descriptor.driver_id)
    (store.directory / f"{credential_id}.json").write_text(
        "fixture-private-corrupt-value", encoding="utf-8"
    )
    status = await service.status(_provider_id(config))
    assert status.state == "unavailable" and status.error_code == "credential_store_error"
    assert "fixture-private" not in status.model_dump_json()


async def test_forget_reports_shared_references_and_removal_does_not_erase_secrets(auth_home):
    config, store, _, service, driver = auth_home()
    started = await begin(service, driver)
    driver.release.set()
    credential = (await service.wait(started.operation_id)).credential
    provider_id = _provider_id(config)
    raw = config.load_raw()
    raw["providers"]["items"]["shared"] = dict(raw["providers"]["items"][provider_id])
    JsonConfigFile(config.root, name="providers.json").write(raw)
    assert (await service.status(provider_id)).bound_provider_ids == tuple(
        sorted((provider_id, "shared"))
    )
    config.remove_provider(provider_id)
    assert store.read(credential.credential_id).state == "active"
    assert (await service.credentials())[0].bound_provider_ids == ("shared",)
    forgotten = await service.forget(credential.credential_id)
    assert forgotten.bound_provider_ids == ("shared",)
    assert (await service.status("shared")).state == "logged_out"
    config.remove_provider("shared")
    assert (await service.forget(credential.credential_id)).bound_provider_ids == ()


async def test_forget_missing_but_bound_record_blocks_pending_login(auth_home):
    config, store, _, service, driver = auth_home()
    provider = config.get_provider(_provider_id(config))
    assert provider is not None
    credential_id = provider.credential_ref.name
    started = await begin(service, driver)
    result = await service.forget(credential_id)
    assert result.credential.revision == 1 and result.credential.state == "logged_out"
    driver.release.set()
    assert (await service.wait(started.operation_id)).error_code == "credential_conflict"
    assert store.read(credential_id).secret_payload == {}


@pytest.mark.parametrize("mode", ["cancel", "timeout", "swallow_timeout", "network_timeout"])
async def test_begin_cancellation_timeout_and_network_failure_release_resources(auth_home, mode):
    class BeginningDriver(ControlledDriver):
        async def begin_authorization(self, method_id):
            self.begin_started.set()
            try:
                if mode == "network_timeout":
                    raise TimeoutError("fixture-secret-network-timeout")
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if mode == "swallow_timeout":
                    return await super().begin_authorization(method_id)
                raise
            finally:
                self.begin_released = True

    driver = BeginningDriver()
    driver.begin_started = asyncio.Event()
    driver.begin_released = False
    _, store, _, service, _ = auth_home(driver, begin_timeout=0.03)
    starting = asyncio.create_task(begin(service, driver))
    await driver.begin_started.wait()
    if mode == "cancel":
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
    else:
        with pytest.raises(AuthError) as error:
            await starting
        assert error.value.code == (
            "authorization_failed" if mode == "network_timeout" else "authorization_expired"
        )
        assert "fixture-secret" not in str(error.value)
    assert driver.begin_released
    assert store.list_ids() == ()
    if mode == "swallow_timeout":
        assert driver.cleaned == 1


async def test_close_reports_cleanup_failure_after_closing_other_operations(auth_home):
    config, _, credentials, _, _ = auth_home()
    driver = ControlledDriver()
    driver.cleanup_error = True
    service = ProviderAuthService(config, credentials, (driver,))
    first = await begin(service, driver)
    second = await begin(service, driver)
    with pytest.raises(AuthError) as error:
        await service.close()
    assert error.value.code == "authorization_cleanup_failed"
    assert driver.cleaned == 2
    assert service.operation(first.operation_id).state == "failed"
    assert service.operation(second.operation_id).state == "failed"


@pytest.mark.parametrize("auth_home", [{"lock_timeout": 0.25}], indirect=True)
async def test_configuration_lock_receives_remaining_credential_wait_budget(auth_home, monkeypatch):
    config, store, _, service, driver = auth_home()
    provider = config.get_provider(_provider_id(config))
    assert provider is not None
    credential_id = provider.credential_ref.name
    started = await begin(service, driver)
    mutation = config.mutation
    credential_lock = store.lock
    clock = [100.0]
    monkeypatch.setattr("ouro.auth.credentials.monotonic", lambda: clock[0])
    monkeypatch.setattr("ouro.auth.commit.monotonic", lambda: clock[0])
    timeouts = []

    @asynccontextmanager
    async def elapsed_credential_lock(credential_id, *, timeout):
        async with credential_lock(credential_id, timeout=timeout):
            clock[0] += 0.15
            yield

    @contextmanager
    def observe_budget(**kwargs):
        timeouts.append(kwargs["timeout"])
        with mutation(**kwargs):
            yield

    with held_config(config):
        monkeypatch.setattr(config, "mutation", observe_budget)
        monkeypatch.setattr(store, "lock", elapsed_credential_lock)
        driver.release.set()
        result = await service.wait(started.operation_id)
    assert result.error_code == "credential_lock_timeout"
    assert timeouts == [pytest.approx(0.25 - 0.15)]
    assert store.read(credential_id) is None


async def test_invalid_config_during_commit_has_safe_diagnostic(auth_home):
    config, store, _, service, driver = auth_home()
    started = await begin(service, driver)
    config.config_path.write_text("fixture-secret-broken-config", encoding="utf-8")
    driver.release.set()
    result = await service.wait(started.operation_id)
    assert result.error_code == "authorization_configuration_error"
    assert "fixture-secret" not in result.model_dump_json()
    assert store.list_ids() == ()
