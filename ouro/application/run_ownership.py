from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import AbortSignal, settle_owned_tasks
from ouro.core.events import EventRecord
from ouro.core.results import RunResult
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, RunStatus, SessionRecord
from ouro.core.tools import ToolResult
from ouro.sessions import SessionLock, SessionStore


@dataclass
class RunReservation:
    """One queued Run's lock, transferred once to its execution attempt."""

    session_id: str
    run_id: str
    _lock: SessionLock | None

    def owns(self, session_id: str, run_id: str) -> bool:
        return self._lock is not None and (session_id, run_id) == (
            self.session_id,
            self.run_id,
        )

    def take(self, session_id: str, run_id: str) -> SessionLock:
        if not self.owns(session_id, run_id):
            raise ValueError("reservation does not own this queued Run")
        lock, self._lock = self._lock, None
        assert lock is not None
        return lock

    def release(self) -> None:
        if self._lock is not None:
            lock, self._lock = self._lock, None
            lock.release()


class RunExecutionOwnership:
    """Own process-local locks and cancellation signals for durable Runs."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self._abort_signals: dict[str, AbortSignal] = {}
        self._abort_states: dict[str, tuple[SessionRecord, RunRecord]] = {}
        self._abort_pollers: dict[str, asyncio.Task[None]] = {}

    async def reserve(self, session_id: str, run_id: str) -> RunReservation | None:
        lock = await self.acquire(session_id, run_id)
        return RunReservation(session_id, run_id, lock) if lock is not None else None

    async def acquire(
        self,
        session_id: str,
        run_id: str,
        *,
        reservation: RunReservation | None = None,
    ) -> SessionLock | None:
        if reservation is not None:
            return reservation.take(session_id, run_id)
        task = asyncio.create_task(
            asyncio.to_thread(self.store.acquire_session_lock, session_id, run_id)
        )
        try:
            lock = await asyncio.shield(task)
        except asyncio.CancelledError:
            await settle_owned_tasks(task)
            if not task.cancelled() and task.exception() is None:
                lock = task.result()
                if lock is not None:
                    await run_blocking(lock.release)
            raise
        return lock

    async def release(self, run_id: str, lock: SessionLock | None) -> None:
        del run_id
        if lock is None:
            return
        await run_blocking(lock.release)

    async def acquire_parked(
        self,
        session_id: str,
        run_id: str,
        *,
        expected_status: RunStatus,
        timeout: float = 5.0,
    ) -> SessionLock | None:
        """Acquire a parked Run after its announcing owner releases the lock.

        The durable interaction can be visible before that owner returns, in any
        Host. Wait only while this same Run remains parked, with a bounded delay;
        the caller must revalidate the exact interaction under the acquired lock.
        """

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            lock = await self.acquire(session_id, run_id)
            if lock is not None:
                return lock
            run = await run_blocking(self.store.load_run, session_id, run_id)
            remaining = deadline - asyncio.get_running_loop().time()
            if run.status != expected_status or remaining <= 0:
                return None
            await asyncio.sleep(min(0.05, remaining))

    async def wait_for_abort_settlement(
        self,
        session_id: str,
        run_id: str,
        *,
        poll_interval: float = 0.05,
    ) -> None:
        """Wait while cancellation is pending and an execution owner is still present."""

        while await run_blocking(self.is_locked, session_id):
            run = await run_blocking(self.store.load_run, session_id, run_id)
            if run.status not in ACTIVE_RUN_STATUSES or not run.abort_requested:
                return
            await asyncio.sleep(poll_interval)

    def is_locked(self, session_id: str) -> bool:
        return self.store.is_session_locked(session_id)

    async def activate_signal(self, session_id: str, run_id: str) -> AbortSignal:
        signal = AbortSignal()
        self._abort_signals[run_id] = signal
        self._abort_states.pop(run_id, None)
        # Close the startup race where a durable abort is requested after the
        # Run starts but before its process-local signal exists.
        abort_state = await run_blocking(self.abort_state, session_id, run_id)
        if abort_state is not None:
            session, run = abort_state
            self.record_abort_state(session, run)
            return signal
        self._abort_pollers[run_id] = asyncio.create_task(
            self._poll_durable_abort(session_id, run_id, signal),
            name=f"ouro-abort-poll-{run_id}",
        )
        return signal

    async def release_signal(self, run_id: str, signal: AbortSignal) -> None:
        if self._abort_signals.get(run_id) is not signal:
            return
        self._abort_signals.pop(run_id, None)
        self._abort_states.pop(run_id, None)
        poller = self._abort_pollers.pop(run_id, None)
        if poller is not None:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)

    def signal_for(self, run_id: str) -> AbortSignal | None:
        return self._abort_signals.get(run_id)

    def is_aborted(self, session_id: str, run_id: str) -> bool:
        del session_id
        signal = self._abort_signals.get(run_id)
        return run_id in self._abort_states or bool(signal and signal.is_aborted())

    def record_abort_state(
        self,
        session: SessionRecord,
        run: RunRecord,
        *,
        reason: str | None = None,
    ) -> None:
        """Publish a durable abort transition to the process-local fast path."""

        self._abort_states[run.run_id] = (session, run)
        signal = self._abort_signals.get(run.run_id)
        if signal is not None:
            signal.abort(reason or _abort_reason(run))

    def abort_state(
        self,
        session_id: str,
        run_id: str,
    ) -> tuple[SessionRecord, RunRecord] | None:
        try:
            run = self.store.load_run(session_id, run_id)
        except Exception:  # noqa: BLE001 - abort polling must not break active runs.
            return None
        if run.status != "aborted" and not run.abort_requested:
            return None
        try:
            return self.store.load_session_record(session_id), run
        except Exception:  # noqa: BLE001 - abort polling must not break active runs.
            return None

    def aborted_result(
        self,
        *,
        session_id: str,
        run_id: str,
        final_text: str,
        emitted: list[EventRecord],
        tool_results: list[ToolResult],
    ) -> RunResult | None:
        del session_id
        abort_state = self._abort_states.get(run_id)
        if abort_state is None:
            return None
        session, run = abort_state
        return RunResult(
            session=session,
            run=run,
            final_text=final_text,
            events=emitted,
            tool_results=tool_results,
        )

    async def _poll_durable_abort(
        self,
        session_id: str,
        run_id: str,
        signal: AbortSignal,
        *,
        poll_interval: float = 0.05,
    ) -> None:
        """Bridge cross-process durable cancellation into the local signal."""

        while self._abort_signals.get(run_id) is signal and not signal.is_aborted():
            await asyncio.sleep(poll_interval)
            abort_state = await run_blocking(self.abort_state, session_id, run_id)
            if abort_state is None:
                continue
            session, run = abort_state
            self.record_abort_state(session, run)
            return


def _abort_reason(run: RunRecord) -> str:
    reason = run.metadata.get("abort_reason")
    return reason if isinstance(reason, str) and reason else "User aborted"
