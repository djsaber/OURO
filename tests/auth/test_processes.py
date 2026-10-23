from __future__ import annotations

import asyncio
import multiprocessing
from pathlib import Path

import ouro.auth.store as credential_store
from ouro.auth.contracts import AuthError, CredentialGrant, CredentialRecord
from ouro.auth.credentials import CredentialService
from ouro.auth.store import FileCredentialStore
from ouro.core.base import new_id


def _refresh_worker(home, credential_id, start, results, counter, contended) -> None:
    try_lock = credential_store.try_lock_descriptor

    def observe_contention(descriptor):
        acquired = try_lock(descriptor)
        if not acquired:
            contended.set()
        return acquired

    credential_store.try_lock_descriptor = observe_contention

    async def run() -> None:
        service = CredentialService(FileCredentialStore(Path(home)), lock_timeout=30)

        async def rotate(record: CredentialRecord) -> CredentialGrant:
            with counter.get_lock():
                counter.value += 1
            assert await asyncio.to_thread(contended.wait, 30)
            return CredentialGrant(secret_payload={"token": "fixture-rotated"})

        results.put(("ready", 0))
        assert await asyncio.to_thread(start.wait, 10)
        try:
            record = await service.refresh(
                credential_id, "fixture", expected_revision=1, refresh=rotate
            )
            results.put(("revision", record.revision))
        finally:
            await service.close()

    asyncio.run(run())


def _commit_worker(home, credential_id, start, results) -> None:
    async def run() -> None:
        service = CredentialService(FileCredentialStore(Path(home)))
        results.put("ready")
        assert await asyncio.to_thread(start.wait, 10)
        try:
            await service.commit(
                credential_id,
                "fixture",
                CredentialGrant(secret_payload={"token": "fixture"}),
                expected_revision=0,
            )
            results.put("committed")
        except AuthError as exc:
            results.put(exc.code)
        finally:
            await service.close()

    asyncio.run(run())


def _hold_lock(home, credential_id, ready) -> None:
    async def run() -> None:
        async with FileCredentialStore(Path(home)).lock(credential_id, timeout=5):
            ready.set()
            await asyncio.Event().wait()

    asyncio.run(run())


def _join(processes) -> None:
    for process in processes:
        process.join(10)
        assert process.exitcode == 0


def _cleanup(processes, queue=None) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()
    if queue is not None:
        queue.close()
        queue.join_thread()


def test_two_processes_exchange_refresh_token_only_once(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start, results, counter = context.Event(), context.Queue(), context.Value("i", 0)
    contended = context.Event()
    credential_id = new_id("cred")
    store = FileCredentialStore(tmp_path)
    store.write(
        CredentialRecord(
            credential_id=credential_id,
            driver_id="fixture",
            revision=1,
            state="active",
            secret_payload={"token": "fixture-original"},
        )
    )
    processes = [
        context.Process(
            target=_refresh_worker,
            args=(str(tmp_path), credential_id, start, results, counter, contended),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        assert [results.get(timeout=10) for _ in processes] == [("ready", 0), ("ready", 0)]
        start.set()
        assert [results.get(timeout=10) for _ in processes] == [("revision", 2), ("revision", 2)]
        _join(processes)
        assert counter.value == 1
        assert store.read(credential_id).revision == 2
    finally:
        _cleanup(processes, results)


def test_two_processes_cannot_commit_the_same_login_revision(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    start, results = context.Event(), context.Queue()
    credential_id = new_id("cred")
    processes = [
        context.Process(target=_commit_worker, args=(str(tmp_path), credential_id, start, results))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        assert [results.get(timeout=10) for _ in processes] == ["ready", "ready"]
        start.set()
        assert sorted(results.get(timeout=10) for _ in processes) == [
            "committed",
            "credential_conflict",
        ]
        _join(processes)
        assert FileCredentialStore(tmp_path).read(credential_id).revision == 1
    finally:
        _cleanup(processes, results)


def test_abnormal_owner_exit_releases_credential_lock(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    credential_id = new_id("cred")
    process = context.Process(target=_hold_lock, args=(str(tmp_path), credential_id, ready))
    try:
        process.start()
        assert ready.wait(10)
        process.terminate()
        process.join(5)
        assert not process.is_alive()

        async def commit() -> None:
            service = CredentialService(FileCredentialStore(tmp_path))
            try:
                record = await service.commit(
                    credential_id,
                    "fixture",
                    CredentialGrant(secret_payload={"token": "fixture"}),
                    expected_revision=0,
                )
                assert record.revision == 1
            finally:
                await service.close()

        asyncio.run(commit())
    finally:
        _cleanup([process])
