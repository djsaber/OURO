from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ouro.auth.contracts import AuthError, CredentialGrant, CredentialIdentity, CredentialRecord
from ouro.auth.credentials import CredentialService
from ouro.auth.store import FileCredentialStore
from ouro.core.base import new_id
from tests.concurrency import wait_until


def grant(value: str = "fixture-original") -> CredentialGrant:
    return CredentialGrant(
        secret_payload={"access": value, "refresh": "fixture-refresh"},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        identity=CredentialIdentity(account_id="fixture-account", display_name="Test account"),
    )


async def initialized(tmp_path: Path):
    service = CredentialService(FileCredentialStore(tmp_path))
    record = await service.commit(new_id("cred"), "test-driver", grant(), expected_revision=0)
    return service, record


async def test_commit_conflict_logout_and_tombstone(tmp_path: Path) -> None:
    service, record = await initialized(tmp_path)
    with pytest.raises(AuthError) as failure:
        await service.commit(record.credential_id, "test-driver", grant(), expected_revision=0)
    assert failure.value.code == "credential_conflict"
    summary = await service.logout(record.credential_id, "test-driver")
    assert summary.revision == 2
    assert summary.state == "logged_out"
    assert summary.identity is None
    assert await service.logout(record.credential_id, "test-driver") == summary
    cleared = await service.read(record.credential_id)
    assert cleared is not None and cleared.secret_payload == {}
    with pytest.raises(AuthError):
        await service.commit(record.credential_id, "test-driver", grant(), expected_revision=1)
    renewed = await service.commit(
        record.credential_id, "test-driver", grant(), expected_revision=2
    )
    assert renewed.revision == 3
    assert "secret" not in (await service.status(record.credential_id)).model_dump_json()
    assert len(await service.list_credentials()) == 1
    await service.close()


async def test_logout_missing_record_prevents_older_login(tmp_path: Path) -> None:
    service = CredentialService(FileCredentialStore(tmp_path))
    credential_id = new_id("cred")
    assert await service.status(credential_id) is None
    await service.logout(credential_id, "test-driver")
    with pytest.raises(AuthError) as failure:
        await service.commit(credential_id, "test-driver", grant(), expected_revision=0)
    assert failure.value.code == "credential_conflict"
    await service.close()


async def test_concurrent_refresh_reloads_under_lock_across_services(tmp_path: Path) -> None:
    first, record = await initialized(tmp_path)
    second = CredentialService(FileCredentialStore(tmp_path))
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def refresh(current: CredentialRecord) -> CredentialGrant:
        nonlocal calls
        calls += 1
        assert current.revision == 1
        started.set()
        await finish.wait()
        return grant("fixture-rotated")

    one = asyncio.create_task(
        first.refresh(record.credential_id, "test-driver", expected_revision=1, refresh=refresh)
    )
    await started.wait()
    two = asyncio.create_task(
        second.refresh(record.credential_id, "test-driver", expected_revision=1, refresh=refresh)
    )
    finish.set()
    results = await asyncio.gather(one, two)
    assert calls == 1
    assert results[0] == results[1]
    assert results[0].revision == 2
    assert results[0].secret_payload["access"] == "fixture-rotated"
    await first.close()
    await second.close()


async def test_cancelled_request_does_not_cancel_exchange_or_other_waiter(tmp_path: Path) -> None:
    service, record = await initialized(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def refresh(current: CredentialRecord) -> CredentialGrant:
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return grant("fixture-rotated")

    first = asyncio.create_task(
        service.refresh(record.credential_id, "test-driver", expected_revision=1, refresh=refresh)
    )
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(
        service.refresh(record.credential_id, "test-driver", expected_revision=1, refresh=refresh)
    )
    finish.set()
    assert (await second).revision == 2
    assert calls == 1
    await service.close()


async def test_logout_waits_for_refresh_then_erases_rotated_secrets(tmp_path: Path) -> None:
    service, record = await initialized(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()

    async def refresh(current: CredentialRecord) -> CredentialGrant:
        started.set()
        await finish.wait()
        return grant("fixture-rotated")

    refresh_task = asyncio.create_task(
        service.refresh(record.credential_id, "test-driver", expected_revision=1, refresh=refresh)
    )
    await started.wait()
    logout_task = asyncio.create_task(service.logout(record.credential_id, "test-driver"))
    finish.set()
    await refresh_task
    assert (await logout_task).revision == 3
    cleared = await service.read(record.credential_id)
    assert cleared.state == "logged_out"
    assert cleared.secret_payload == {}
    with pytest.raises(AuthError) as failure:
        await service.refresh(
            record.credential_id, "test-driver", expected_revision=1, refresh=refresh
        )
    assert failure.value.code == "authentication_required"
    await service.close()


@pytest.mark.parametrize("permanent", [False, True])
async def test_refresh_failure_classification_and_preservation(tmp_path: Path, permanent: bool):
    service, record = await initialized(tmp_path)

    async def refresh(current: CredentialRecord) -> CredentialGrant:
        if permanent:
            raise AuthError("authentication_required")
        raise OSError("Authorization: fixture-secret")

    with pytest.raises(AuthError) as failure:
        await service.refresh(
            record.credential_id, "test-driver", expected_revision=1, refresh=refresh
        )
    assert "fixture-secret" not in str(failure.value)
    latest = await service.read(record.credential_id)
    assert latest.secret_payload == record.secret_payload
    assert latest.state == ("reauth_required" if permanent else "active")
    assert latest.revision == (2 if permanent else 1)
    assert failure.value.code == (
        "authentication_required" if permanent else "credential_refresh_failed"
    )
    await service.close()


async def test_old_revision_refresh_never_overwrites_new_login(tmp_path: Path) -> None:
    service, record = await initialized(tmp_path)
    replacement = await service.commit(
        record.credential_id, "test-driver", grant("new-login"), expected_revision=1
    )

    async def must_not_refresh(current: CredentialRecord) -> CredentialGrant:
        pytest.fail("stale request must not refresh new login material")

    actual = await service.refresh(
        record.credential_id, "test-driver", expected_revision=1, refresh=must_not_refresh
    )
    assert actual == replacement
    await service.close()


async def test_refresh_is_bounded_and_lock_released(tmp_path: Path) -> None:
    service, record = await initialized(tmp_path)
    bounded = CredentialService(FileCredentialStore(tmp_path), refresh_timeout=0.03)

    async def never(current: CredentialRecord) -> CredentialGrant:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with pytest.raises(AuthError) as failure:
        await bounded.refresh(
            record.credential_id, "test-driver", expected_revision=1, refresh=never
        )
    assert failure.value.code == "credential_refresh_failed"
    assert await service.read(record.credential_id) == record
    await service.logout(record.credential_id, "test-driver")
    await bounded.close()
    await service.close()


async def test_cancel_login_waiting_for_lock_does_not_commit(tmp_path: Path) -> None:
    store = FileCredentialStore(tmp_path)
    service = CredentialService(store)
    credential_id = new_id("cred")
    async with store.lock(credential_id, timeout=1):
        task = asyncio.create_task(
            service.commit(credential_id, "test-driver", grant(), expected_revision=0)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await service.status(credential_id) is None
    await service.close()


async def test_cancel_after_atomic_write_starts_reports_committed_result(tmp_path, monkeypatch):
    store = FileCredentialStore(tmp_path)
    service = CredentialService(store)
    started, finish = threading.Event(), threading.Event()
    write = store.write

    def slow_write(record: CredentialRecord) -> None:
        started.set()
        assert finish.wait(5)
        write(record)

    monkeypatch.setattr(store, "write", slow_write)
    credential_id = new_id("cred")
    task = asyncio.create_task(
        service.commit(credential_id, "test-driver", grant(), expected_revision=0)
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    finish.set()
    actual = await task
    assert actual.revision == 1
    assert await service.read(credential_id) == actual
    await service.close()


async def test_shutdown_drains_exchange_after_request_cancellation(tmp_path: Path) -> None:
    service, record = await initialized(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()

    async def refresh(current: CredentialRecord) -> CredentialGrant:
        started.set()
        await finish.wait()
        return grant("shutdown-rotation")

    task = asyncio.create_task(
        service.refresh(record.credential_id, "test-driver", expected_revision=1, refresh=refresh)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    closing.cancel()
    finish.set()
    await closing
    assert FileCredentialStore(tmp_path).read(record.credential_id).revision == 2
    with pytest.raises(AuthError) as failure:
        await service.status(record.credential_id)
    assert failure.value.code == "auth_closed"
    await service.close()


async def test_two_drivers_share_lifecycle_without_sharing_payload_shape(tmp_path: Path) -> None:
    service, first = await initialized(tmp_path)
    second = await service.commit(
        new_id("cred"),
        "other-driver",
        CredentialGrant(secret_payload={"session": {"cookie": "fixture-cookie"}}),
        expected_revision=0,
    )

    async def refresh_other(current: CredentialRecord) -> CredentialGrant:
        assert current.secret_payload == {"session": {"cookie": "fixture-cookie"}}
        return CredentialGrant(secret_payload={"session": {"cookie": "fixture-new-cookie"}})

    renewed = await service.refresh(
        second.credential_id, "other-driver", expected_revision=1, refresh=refresh_other
    )
    assert renewed.revision == 2
    assert await service.read(first.credential_id) == first
    with pytest.raises(AuthError) as failure:
        await service.logout(first.credential_id, "other-driver")
    assert failure.value.code == "credential_conflict"
    await service.close()


async def test_rotated_grant_save_failure_is_not_reported_as_success(tmp_path, monkeypatch) -> None:
    service, record = await initialized(tmp_path)
    failing_store = FileCredentialStore(tmp_path)
    failing = CredentialService(failing_store)
    calls = 0

    async def refresh(current: CredentialRecord) -> CredentialGrant:
        nonlocal calls
        calls += 1
        return grant("already-rotated-remotely")

    def fail_write(record: CredentialRecord) -> None:
        raise AuthError("credential_store_error")

    monkeypatch.setattr(failing_store, "write", fail_write)
    with pytest.raises(AuthError) as failure:
        await failing.refresh(
            record.credential_id, "test-driver", expected_revision=1, refresh=refresh
        )
    assert failure.value.code == "credential_store_error"
    assert calls == 1
    assert await service.read(record.credential_id) == record
    await failing.close()
    await service.close()


async def test_close_waits_for_atomic_commit_and_lock_release(tmp_path, monkeypatch) -> None:
    store = FileCredentialStore(tmp_path)
    service = CredentialService(store, refresh_timeout=0.02)
    started, finish = threading.Event(), threading.Event()
    write = store.write

    def slow_write(record: CredentialRecord) -> None:
        started.set()
        finish.wait()
        write(record)

    monkeypatch.setattr(store, "write", slow_write)
    credential_id = new_id("cred")
    commit = asyncio.create_task(
        service.commit(credential_id, "test-driver", grant(), expected_revision=0)
    )
    close = None
    try:
        assert await asyncio.to_thread(started.wait, 30)
        close = asyncio.create_task(service.close())
        await wait_until(
            lambda: any(task.cancelling() for task in service._operations),
            description="shutdown cancellation reaching the active commit",
        )
        assert not close.done()
        finish.set()
        await close
        assert (await commit).revision == 1
        async with store.lock(credential_id, timeout=0.1):
            assert store.read(credential_id).revision == 1
    finally:
        finish.set()
        await asyncio.gather(commit, *([close] if close else []), return_exceptions=True)
        await service.close()


async def test_shutdown_during_snapshot_read_cannot_admit_late_commit(
    tmp_path, monkeypatch
) -> None:
    store = FileCredentialStore(tmp_path)
    service = CredentialService(store)
    started, finish = threading.Event(), threading.Event()
    read = store.read

    def slow_read(credential_id: str):
        started.set()
        assert finish.wait(5)
        return read(credential_id)

    monkeypatch.setattr(store, "read", slow_read)
    credential_id = new_id("cred")
    commit = asyncio.create_task(
        service.commit(credential_id, "test-driver", grant(), expected_revision=0)
    )
    assert await asyncio.to_thread(started.wait, 5)
    close = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    finish.set()
    await close
    with pytest.raises(AuthError) as failure:
        await commit
    assert failure.value.code == "auth_closed"
    assert read(credential_id) is None


async def test_future_revision_cannot_hide_record_rollback(tmp_path: Path) -> None:
    service, record = await initialized(tmp_path)

    async def must_not_refresh(current: CredentialRecord) -> CredentialGrant:
        pytest.fail("revision regression must be rejected")

    with pytest.raises(AuthError) as failure:
        await service.refresh(
            record.credential_id, "test-driver", expected_revision=2, refresh=must_not_refresh
        )
    assert failure.value.code == "credential_conflict"
    assert await service.read(record.credential_id) == record
    await service.close()


async def test_binding_guard_encloses_save_and_is_entered_after_credential_lock(
    tmp_path, monkeypatch
) -> None:
    store = FileCredentialStore(tmp_path)
    service = CredentialService(store)
    credential_id = new_id("cred")
    inside_guard = False
    write = store.write

    thread_id = None

    @contextmanager
    def guard(*, timeout, cancel_event):
        nonlocal inside_guard
        nonlocal thread_id
        thread_id = threading.get_ident()
        assert thread_id != threading.main_thread().ident

        async def check_lock():
            async with store.lock(credential_id, timeout=0.02):
                pytest.fail("credential lock must already be held")

        with pytest.raises(AuthError) as failure:
            asyncio.run(check_lock())
        assert failure.value.code == "credential_lock_timeout"
        inside_guard = True
        try:
            yield
        finally:
            assert threading.get_ident() == thread_id
            inside_guard = False

    def checked_write(record):
        assert inside_guard
        assert threading.get_ident() == thread_id
        write(record)

    monkeypatch.setattr(store, "write", checked_write)
    await service.commit(
        credential_id, "test-driver", grant(), expected_revision=0, binding_guard=guard
    )
    assert not inside_guard
    await service.close()


@pytest.mark.parametrize("conflict", [False, True])
async def test_binding_guard_conflict_or_timeout_preserves_existing_credential(tmp_path, conflict):
    original, record = await initialized(tmp_path)
    service = CredentialService(FileCredentialStore(tmp_path), lock_timeout=0.04)

    @contextmanager
    def guard(*, timeout, cancel_event):
        if conflict:
            raise AuthError("credential_conflict")
        cancel_event.wait(timeout)
        raise AuthError("credential_lock_timeout")
        yield

    with pytest.raises(AuthError) as failure:
        await service.commit(
            record.credential_id,
            "test-driver",
            grant("new-login"),
            expected_revision=1,
            binding_guard=guard,
        )
    assert failure.value.code == ("credential_conflict" if conflict else "credential_lock_timeout")
    assert await original.read(record.credential_id) == record
    await service.close()
    await original.close()


async def test_cancel_during_commit_read_waits_for_worker_and_prevents_write(tmp_path, monkeypatch):
    store = FileCredentialStore(tmp_path)
    service = CredentialService(store)
    entered, release = threading.Event(), threading.Event()
    original_read = store.read

    def read(credential_id):
        entered.set()
        assert release.wait(5)
        return original_read(credential_id)

    monkeypatch.setattr(store, "read", read)
    credential_id = new_id("cred")
    task = asyncio.create_task(
        service.commit(credential_id, "test-driver", grant(), expected_revision=0)
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    with pytest.raises(AuthError):
        async with store.lock(credential_id, timeout=0.02):
            pytest.fail("cancelled worker must still own the credential lock")
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert original_read(credential_id) is None
    await service.close()


@pytest.mark.parametrize("stop", ["cancel", "close", "deadline"])
async def test_queued_commit_worker_cannot_write_after_cancellation_or_deadline(
    tmp_path, monkeypatch, stop
):
    store = FileCredentialStore(tmp_path)
    service = CredentialService(store, lock_timeout=30)
    clock = [100.0]
    monkeypatch.setattr("ouro.auth.credentials.monotonic", lambda: clock[0])
    monkeypatch.setattr("ouro.auth.commit.monotonic", lambda: clock[0])
    loop = asyncio.get_running_loop()
    acquired = asyncio.Event()
    busy, release = threading.Event(), threading.Event()
    original_lock = store.lock

    @asynccontextmanager
    async def observed_lock(credential_id, *, timeout):
        async with original_lock(credential_id, timeout=timeout):
            acquired.set()
            yield

    def occupy_worker():
        busy.set()
        assert release.wait(5)

    def must_not_write(record):
        pytest.fail("a queued commit must recheck admission before writing")

    monkeypatch.setattr(store, "lock", observed_lock)
    monkeypatch.setattr(store, "write", must_not_write)
    executor = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(executor)
    occupied = loop.run_in_executor(None, occupy_worker)
    assert busy.wait(5)
    credential_id = new_id("cred")
    task = asyncio.create_task(
        service.commit(
            credential_id,
            "test-driver",
            grant(),
            expected_revision=0,
        )
    )
    closing = None
    try:
        await acquired.wait()
        if stop == "cancel":
            task.cancel()
        elif stop == "close":
            closing = asyncio.create_task(service.close())
        else:
            clock[0] += 31
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    await occupied
    if stop == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(AuthError) as error:
            await task
        assert error.value.code == ("auth_closed" if stop == "close" else "credential_lock_timeout")
    if closing is not None:
        await closing
    assert store.read(credential_id) is None
    await service.close()
