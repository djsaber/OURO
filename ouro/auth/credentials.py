from __future__ import annotations

import asyncio
import math
from collections.abc import Coroutine
from contextlib import nullcontext
from time import monotonic
from typing import Any

from ouro.auth.commit import CommitAdmission
from ouro.auth.contracts import (
    AuthError,
    CommitGuard,
    CredentialGrant,
    CredentialRecord,
    CredentialStore,
    CredentialSummary,
    RefreshCredential,
)


class CredentialService:
    """Serialize lifecycle writes without owning Provider configuration or HTTP."""

    def __init__(
        self,
        store: CredentialStore,
        *,
        lock_timeout: float = 10.0,
        refresh_timeout: float = 60.0,
    ) -> None:
        if any(not math.isfinite(value) or value <= 0 for value in (lock_timeout, refresh_timeout)):
            raise ValueError("credential timeouts must be positive")
        self._store = store
        self._lock_timeout = lock_timeout
        self._refresh_timeout = refresh_timeout
        self._operations: set[asyncio.Task[object]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._commits: set[CommitAdmission] = set()

    async def read(self, credential_id: str) -> CredentialRecord | None:
        """Internal material boundary. Hosts must use status/list_credentials."""
        self._require_open()
        return await asyncio.to_thread(self._store.read, credential_id)

    async def status(self, credential_id: str) -> CredentialSummary | None:
        record = await self.read(credential_id)
        return CredentialSummary.from_record(record) if record is not None else None

    async def list_credentials(self) -> tuple[CredentialSummary, ...]:
        self._require_open()
        ids = await asyncio.to_thread(self._store.list_ids)
        summaries = [await self.status(credential_id) for credential_id in ids]
        return tuple(summary for summary in summaries if summary is not None)

    async def commit(
        self,
        credential_id: str,
        driver_id: str,
        grant: CredentialGrant,
        *,
        expected_revision: int,
        binding_guard: CommitGuard | None = None,
    ) -> CredentialRecord:
        """Commit a login grant; missing records have revision zero, never reused."""
        self._require_open()
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        grant = grant.model_copy(deep=True)
        return await self._start(
            self._commit(credential_id, driver_id, grant, expected_revision, binding_guard)
        )

    async def _commit(
        self,
        credential_id: str,
        driver_id: str,
        grant: CredentialGrant,
        expected_revision: int,
        binding_guard: CommitGuard | None,
    ) -> CredentialRecord:
        admission = CommitAdmission(monotonic() + self._lock_timeout)
        async with self._store.lock(credential_id, timeout=admission.check()):
            self._require_open()
            self._commits.add(admission)
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self._commit_sync,
                    credential_id,
                    driver_id,
                    grant,
                    expected_revision,
                    binding_guard,
                    admission,
                )
            )
            try:
                while True:
                    try:
                        return await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        admission.cancel()
                        # A queued/running worker must finish before its credential
                        # lock is released, even when cancellation wins admission.
                        if worker.done():
                            return worker.result()
            finally:
                self._commits.discard(admission)

    def _commit_sync(
        self,
        credential_id: str,
        driver_id: str,
        grant: CredentialGrant,
        expected_revision: int,
        binding_guard: CommitGuard | None,
        admission: CommitAdmission,
    ) -> CredentialRecord:
        boundary = (
            binding_guard(timeout=admission.check(), cancel_event=admission.cancel_event)
            if binding_guard is not None
            else nullcontext()
        )
        try:
            with boundary:
                admission.check()
                current = self._store.read(credential_id)
                revision = current.revision if current is not None else 0
                if revision != expected_revision or (
                    current is not None and current.driver_id != driver_id
                ):
                    raise AuthError("credential_conflict")
                record = self._record(credential_id, driver_id, revision + 1, grant)
                admission.start()
                self._store.write(record)
                return record
        except InterruptedError:
            admission.check()
            raise

    async def logout(self, credential_id: str, driver_id: str) -> CredentialSummary:
        """Also used by forget: a tombstone prevents an older login resurrecting secrets."""
        self._require_open()
        return await self._start(self._logout(credential_id, driver_id))

    async def _logout(self, credential_id: str, driver_id: str) -> CredentialSummary:
        async with self._store.lock(credential_id, timeout=self._lock_timeout):
            self._require_open()
            current = await asyncio.to_thread(self._store.read, credential_id)
            self._require_open()
            if current is not None and current.driver_id != driver_id:
                raise AuthError("credential_conflict")
            if current is not None and current.state == "logged_out":
                return CredentialSummary.from_record(current)
            record = CredentialRecord(
                credential_id=credential_id,
                driver_id=driver_id,
                revision=1 if current is None else current.revision + 1,
                state="logged_out",
            )
            await self._save(record)
            return CredentialSummary.from_record(record)

    async def refresh(
        self,
        credential_id: str,
        driver_id: str,
        *,
        expected_revision: int,
        refresh: RefreshCredential,
    ) -> CredentialRecord:
        """A caller cancels only its wait; service owns the exchange and durable save."""
        self._require_open()
        if expected_revision < 1:
            raise ValueError("refresh requires a persisted credential revision")
        task = self._start(self._refresh(credential_id, driver_id, expected_revision, refresh))
        return await asyncio.shield(task)

    async def _refresh(
        self,
        credential_id: str,
        driver_id: str,
        expected_revision: int,
        refresh: RefreshCredential,
    ) -> CredentialRecord:
        try:
            async with asyncio.timeout(self._refresh_timeout):
                async with self._store.lock(credential_id, timeout=self._lock_timeout):
                    current = await asyncio.to_thread(self._store.read, credential_id)
                    if current is None or current.state != "active":
                        raise AuthError("authentication_required")
                    if current.driver_id != driver_id:
                        raise AuthError("credential_conflict")
                    if current.revision < expected_revision:
                        raise AuthError("credential_conflict")
                    if current.revision > expected_revision:
                        return current
                    try:
                        grant = await refresh(current)
                    except AuthError as exc:
                        if exc.code == "authentication_required":
                            await self._save(
                                current.model_copy(
                                    update={
                                        "revision": current.revision + 1,
                                        "state": "reauth_required",
                                    }
                                )
                            )
                        raise
                    record = self._record(credential_id, driver_id, current.revision + 1, grant)
                    await self._save(record)
                    return record
        except AuthError:
            raise
        except Exception:
            # Driver exceptions can include a token response or request URL.
            raise AuthError("credential_refresh_failed") from None

    async def close(self) -> None:
        """Stop admission, then finish or cancel bounded owned exchanges."""
        self._closed = True
        for admission in self._commits:
            admission.cancel("auth_closed")
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        while True:
            try:
                await asyncio.shield(self._close_task)
                return
            except asyncio.CancelledError:
                if self._close_task.done():
                    self._close_task.result()
                    return

    async def _close(self) -> None:
        tasks = tuple(self._operations)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=self._refresh_timeout)
        for task in pending:
            task.cancel()
        # Threaded atomic writes cannot be cancelled. They must finish before
        # any owning refresh releases its lock or the service reports closure.
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _save(self, record: CredentialRecord) -> None:
        # Once atomic replacement starts, cancellation cannot undo it. Finish the
        # worker before releasing the lock and report the actual persistence result.
        task = asyncio.create_task(asyncio.to_thread(self._store.write, record))
        while True:
            try:
                await asyncio.shield(task)
                return
            except asyncio.CancelledError:
                if task.done():
                    task.result()
                    return

    def _start[T](self, operation: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(operation)
        self._operations.add(task)
        task.add_done_callback(self._operation_finished)
        return task

    def _operation_finished(self, task: asyncio.Task[object]) -> None:
        self._operations.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve failures even when every caller cancelled its wait.

    def _require_open(self) -> None:
        if self._closed:
            raise AuthError("auth_closed")

    @staticmethod
    def _record(
        credential_id: str, driver_id: str, revision: int, grant: CredentialGrant
    ) -> CredentialRecord:
        return CredentialRecord(
            credential_id=credential_id,
            driver_id=driver_id,
            revision=revision,
            state="active",
            expires_at=grant.expires_at,
            identity=grant.identity,
            secret_payload=grant.secret_payload,
        )
