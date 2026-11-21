"""Acquire a Session for one input, commit its start and execute its Driver."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ouro.application.models.catalog import SessionModels
from ouro.application.runs.execution import RunDriverExecution
from ouro.application.runs.lifecycle import maybe_active_run, require_current_continuation
from ouro.application.runs.ownership import RunExecutionOwnership, RunReservation
from ouro.application.runs.recovery import RunRecoveryService
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal
from ouro.core.errors import SessionBusyError
from ouro.core.results import RunResult
from ouro.core.session_ports import RuntimeSessionSnapshot
from ouro.core.state import RunRecord, SessionRecord

if TYPE_CHECKING:
    from ouro.application.runtime.extensions import RuntimeExtensions
    from ouro.core.state import SessionContinuationRequest


class QueuedInputSelectionChanged(RuntimeError):
    """Another owner changed which accepted input can start."""


class RunStartExecution:
    def __init__(
        self,
        ownership: RunExecutionOwnership,
        recovery: RunRecoveryService,
        models: SessionModels,
        driver: RunDriverExecution,
        *,
        release_resources: Callable[[str], None],
        finalize: Callable[[RunResult], Awaitable[RunResult]],
    ) -> None:
        self.ownership = ownership
        self.recovery = recovery
        self.models = models
        self.driver = driver
        self.release_resources = release_resources
        self.finalize = finalize

    async def execute(
        self,
        snapshot: RuntimeSessionSnapshot,
        run: RunRecord,
        *,
        start: Callable[
            [RuntimeSessionSnapshot, SessionRecord, RunRecord],
            Awaitable[tuple[SessionRecord, RunRecord]],
        ],
        enqueue: Callable[[RuntimeSessionSnapshot, RunRecord], Awaitable[RunResult]] | None,
        extensions: RuntimeExtensions | None,
        reservation: RunReservation | None = None,
        continuation_request: SessionContinuationRequest | None = None,
        on_provider_stream_start: Callable[[], None] | None = None,
    ) -> RunResult:
        store = self.ownership.store
        session_id = snapshot.session.session_id

        def competing(current: RuntimeSessionSnapshot) -> RunRecord | None:
            require_current_continuation(current.session, continuation_request)
            if pending := current.pending_continuation:
                raise SessionBusyError(
                    f"session has an unresolved terminal Run continuation: {pending.run_id}"
                )
            active = maybe_active_run(current.runs, history_epoch=current.session.history_epoch)
            if active is not None and (run.status != "queued" or active.run_id != run.run_id):
                return active
            return None

        async def busy(current: RuntimeSessionSnapshot, active: RunRecord) -> RunResult:
            if enqueue is not None:
                return await enqueue(current, active)
            return RunResult(session=current.session, run=active)

        if active := competing(snapshot):
            return await busy(snapshot, active)
        lock = await self.ownership.acquire(session_id, run.run_id, reservation=reservation)
        if lock is None:
            if enqueue is None:
                await self.ownership.wait_for_run_start(session_id)
            latest = await run_blocking(store.load_runtime_session, session_id)
            if active := competing(latest):
                return await busy(latest, active)
            if enqueue is None:
                raise QueuedInputSelectionChanged
            raise SessionBusyError(f"session is already locked: {session_id}")

        signal: AbortSignal | None = None
        result: RunResult | None = None
        try:
            current = snapshot
            if await run_blocking(store.load_session_record, session_id) != snapshot.session:
                current = await run_blocking(store.load_runtime_session, session_id)
            current = await self.recovery.reconcile(current)
            if current is not snapshot and (active := competing(current)):
                return await busy(current, active)
            session, adapter = await run_blocking(self.models.bind_session, session_id)
            session, run = await start(
                current, session, run.model_copy(update={"model": adapter.model.model_id})
            )
            signal = await self.ownership.activate_signal(session_id, run.run_id)
            result = await self.driver.execute(
                extensions=extensions,
                session=session,
                run=run,
                abort_signal=signal,
                adapter=adapter,
                on_provider_stream_start=on_provider_stream_start,
            )
        finally:
            try:
                if signal is not None:
                    await self.ownership.release_signal(run.run_id, signal)
            finally:
                try:
                    if result is None:
                        self.release_resources(run.run_id)
                finally:
                    await self.ownership.release(run.run_id, lock)
        return await self.finalize(result)
