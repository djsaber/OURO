from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final, Protocol

from ouro.application.dto import Diagnostic
from ouro.application.runs import maybe_active_run
from ouro.application.runtime import RuntimeService
from ouro.application.supervisor_models import (
    RunHandle,
    RuntimeNotification,
    RuntimeStatusSnapshot,
    ShutdownPolicy,
    SupervisorRunStatus,
    SupervisorStartResult,
)
from ouro.core.base import utc_now
from ouro.core.errors import OuroError, SessionBusyError, SessionNotFoundError
from ouro.core.events import EventRecord
from ouro.core.results import RunResult

_CLOSED: Final = object()


class RuntimeManagedResource(Protocol):
    """A long-lived process resource owned by RuntimeSupervisor."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...


@dataclass
class _RunJob:
    handle: RunHandle
    prompt: str
    title: str | None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: RunResult | None = None
    cancelled: bool = False


class RuntimeSubscription(AsyncIterator[RuntimeNotification]):
    """Bounded non-blocking view of one Session's durable runtime events."""

    def __init__(
        self,
        supervisor: RuntimeSupervisor,
        session_id: str,
        *,
        max_buffer: int,
    ) -> None:
        self.session_id = session_id
        self._supervisor = supervisor
        self._queue: asyncio.Queue[RuntimeNotification | object] = asyncio.Queue(max_buffer)
        self._closed = False

    def __aiter__(self) -> RuntimeSubscription:
        return self

    async def __anext__(self) -> RuntimeNotification:
        item = await self._queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        assert isinstance(item, RuntimeNotification)
        return item

    async def get(self) -> RuntimeNotification:
        return await self.__anext__()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._supervisor.unsubscribe(self)
        self._offer_closed()

    def _offer(self, notification: RuntimeNotification) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(notification)
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            diagnostic = Diagnostic(
                code="runtime_subscription_lagged",
                severity="warning",
                component="runtime.supervisor",
                message="subscriber buffer overflowed; one or more events were skipped",
                session_id=self.session_id,
                recoverable=True,
                suggested_action="Reload durable events from the SessionStore.",
            )
            self._queue.put_nowait(RuntimeNotification(kind="diagnostic", diagnostic=diagnostic))

    def _offer_closed(self) -> None:
        try:
            self._queue.put_nowait(_CLOSED)
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self._queue.put_nowait(_CLOSED)


class RuntimeSupervisor:
    """Own cross-Run scheduling and Host subscriptions, not durable Run transitions."""

    def __init__(
        self,
        runtime: RuntimeService,
        *,
        max_concurrency: int = 4,
        managed_resources: Iterable[RuntimeManagedResource] = (),
    ) -> None:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")
        self.runtime = runtime
        self.store = runtime.store
        self.max_concurrency = max_concurrency
        self._managed_resources = tuple(managed_resources)
        self._started_resources: list[RuntimeManagedResource] = []
        self._queue: asyncio.Queue[_RunJob | None] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._jobs_by_session: dict[str, _RunJob] = {}
        self._jobs_by_handle: dict[str, _RunJob] = {}
        self._completion_events: dict[str, asyncio.Event] = {}
        self._snapshots: dict[str, RuntimeStatusSnapshot] = {}
        self._subscriptions: dict[str, set[RuntimeSubscription]] = {}
        self._started = False
        self._closed = False
        self.runtime.add_event_sink(self._on_event)

    async def start(self) -> SupervisorStartResult:
        if self._closed:
            raise OuroError("runtime supervisor is closed")
        if self._started:
            return SupervisorStartResult()
        try:
            for resource in self._managed_resources:
                await resource.start()
                self._started_resources.append(resource)
            recovered: list[RuntimeStatusSnapshot] = []
            for session in self.store.list_sessions():
                snapshot = self.store.load_session(session.session_id, recover=True)
                for run in snapshot.runs:
                    if run.status == "recoverable":
                        recovered.append(
                            RuntimeStatusSnapshot(
                                session_id=session.session_id,
                                run_id=run.run_id,
                                status="recoverable",
                            )
                        )
            self._workers = [
                asyncio.create_task(self._worker(index), name=f"ouro-runtime-{index}")
                for index in range(self.max_concurrency)
            ]
        except Exception:
            await asyncio.gather(
                *(resource.close() for resource in reversed(self._started_resources)),
                return_exceptions=True,
            )
            self._started_resources.clear()
            raise
        self._started = True
        return SupervisorStartResult(recovered=tuple(recovered))

    async def submit(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        title: str | None = None,
    ) -> RunHandle:
        await self.start()
        if not prompt.strip():
            raise ValueError("run prompt cannot be empty")
        if session_id is None:
            created = self.runtime.sessions.create(
                title=title or _title_from_prompt(prompt),
                current_model=self.runtime.current_model().key,
            )
            session_id = created.session.session_id
        else:
            try:
                self.store.load_session_record(session_id)
            except FileNotFoundError as exc:
                raise SessionNotFoundError(f"session not found: {session_id}") from exc
        if session_id in self._jobs_by_session:
            raise SessionBusyError(f"session already has a supervised run: {session_id}")
        durable = self.store.load_session(session_id, recover=False)
        if maybe_active_run(durable.runs) is not None:
            raise SessionBusyError(f"session already has an active run: {session_id}")

        handle = RunHandle(session_id=session_id)
        job = _RunJob(handle=handle, prompt=prompt, title=title)
        self._jobs_by_session[session_id] = job
        self._jobs_by_handle[handle.handle_id] = job
        self._completion_events[handle.handle_id] = job.done
        self._snapshots[handle.handle_id] = RuntimeStatusSnapshot(
            handle=handle,
            session_id=session_id,
            status="queued",
        )
        self._queue.put_nowait(job)
        return handle

    def subscribe(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        max_buffer: int = 256,
    ) -> RuntimeSubscription:
        if self._closed:
            raise OuroError("runtime supervisor is closed")
        if max_buffer <= 0:
            raise ValueError("max_buffer must be positive")
        try:
            snapshot = self.store.load_session(session_id, recover=False)
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc
        subscription = RuntimeSubscription(self, session_id, max_buffer=max_buffer)
        self._subscriptions.setdefault(session_id, set()).add(subscription)
        for event in sorted(snapshot.events, key=lambda item: (item.sequence, item.event_id)):
            if event.sequence > after_sequence:
                subscription._offer(RuntimeNotification(kind="event", event=event))
        return subscription

    def unsubscribe(self, subscription: RuntimeSubscription) -> None:
        subscriptions = self._subscriptions.get(subscription.session_id)
        if subscriptions is None:
            return
        subscriptions.discard(subscription)
        if not subscriptions:
            self._subscriptions.pop(subscription.session_id, None)

    def status(self, handle_or_session: RunHandle | str) -> RuntimeStatusSnapshot:
        if isinstance(handle_or_session, RunHandle):
            snapshot = self._snapshots.get(handle_or_session.handle_id)
            if snapshot is None:
                raise KeyError(f"unknown run handle: {handle_or_session.handle_id}")
            return snapshot
        job = self._jobs_by_handle.get(handle_or_session)
        if job is not None:
            return self._snapshots[job.handle.handle_id]
        completed = self._snapshots.get(handle_or_session)
        if completed is not None:
            return completed
        active = self._jobs_by_session.get(handle_or_session)
        if active is not None:
            return self._snapshots[active.handle.handle_id]
        try:
            durable_snapshot = self.store.load_session(handle_or_session, recover=False)
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {handle_or_session}") from exc
        run = maybe_active_run(durable_snapshot.runs)
        if run is None and durable_snapshot.session.last_run_id is not None:
            run = next(
                (
                    item
                    for item in durable_snapshot.runs
                    if item.run_id == durable_snapshot.session.last_run_id
                ),
                None,
            )
        return RuntimeStatusSnapshot(
            session_id=handle_or_session,
            run_id=run.run_id if run is not None else None,
            status=(run.status if run is not None else "completed"),
        )

    async def wait(self, handle: RunHandle) -> RuntimeStatusSnapshot:
        done = self._completion_events.get(handle.handle_id)
        if done is None:
            raise KeyError(f"unknown run handle: {handle.handle_id}")
        await done.wait()
        return self.status(handle)

    async def abort(
        self,
        handle_or_session: RunHandle | str,
        *,
        reason: str = "User aborted",
    ) -> RuntimeStatusSnapshot:
        try:
            job = self._resolve_job(handle_or_session)
        except KeyError:
            if isinstance(handle_or_session, RunHandle):
                return self.status(handle_or_session)
            completed = self._snapshots.get(handle_or_session)
            if completed is not None:
                return completed
            raise
        snapshot = self._snapshots[job.handle.handle_id]
        if snapshot.status == "queued":
            job.cancelled = True
            self._finish_job(
                job,
                RuntimeStatusSnapshot(
                    handle=job.handle,
                    session_id=job.handle.session_id,
                    status="aborted",
                    diagnostics=(
                        Diagnostic(
                            code="queued_run_aborted",
                            severity="info",
                            component="runtime.supervisor",
                            message=reason,
                            session_id=job.handle.session_id,
                            recoverable=False,
                        ),
                    ),
                ),
            )
            return self.status(job.handle)
        if snapshot.status in {"running", "waiting_user", "waiting_approval"}:
            result = await self.runtime.abort_run(
                session_id=job.handle.session_id,
                run_id=snapshot.run_id,
                reason=reason,
            )
            updated = RuntimeStatusSnapshot(
                handle=job.handle,
                session_id=job.handle.session_id,
                run_id=result.run.run_id,
                status=result.run.status,
            )
            self._snapshots[job.handle.handle_id] = updated
        return self.status(job.handle)

    async def shutdown(self, policy: ShutdownPolicy | None = None) -> None:
        if self._closed:
            return
        policy = policy or ShutdownPolicy()
        self._closed = True
        jobs = list(self._jobs_by_session.values())
        if policy.mode == "abort":
            for job in jobs:
                with suppress(OuroError, KeyError):
                    await self.abort(job.handle, reason="Runtime supervisor is shutting down")
        try:
            async with asyncio.timeout(policy.timeout_seconds):
                await asyncio.gather(*(job.done.wait() for job in jobs))
        except TimeoutError:
            for worker in self._workers:
                worker.cancel()
        for _ in self._workers:
            self._queue.put_nowait(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        for job in jobs:
            if job.done.is_set():
                continue
            current = self._snapshots[job.handle.handle_id]
            if current.status == "queued":
                job.cancelled = True
                self._finish_job(
                    job,
                    RuntimeStatusSnapshot(
                        handle=job.handle,
                        session_id=job.handle.session_id,
                        status="aborted",
                        diagnostics=(
                            Diagnostic(
                                code="queued_run_cancelled_on_shutdown",
                                severity="warning",
                                component="runtime.supervisor",
                                message="queued run did not start before shutdown timeout",
                                session_id=job.handle.session_id,
                                recoverable=False,
                            ),
                        ),
                    ),
                )
            else:
                self._finish_interrupted_job(job)
        for subscriptions in tuple(self._subscriptions.values()):
            for subscription in tuple(subscriptions):
                subscription.close()
        self._workers.clear()
        close_results = await asyncio.gather(
            *(resource.close() for resource in reversed(self._started_resources)),
            return_exceptions=True,
        )
        self._started_resources.clear()
        close_errors = [result for result in close_results if isinstance(result, Exception)]
        if close_errors:
            raise ExceptionGroup("runtime managed resource shutdown failed", close_errors)

    async def _worker(self, index: int) -> None:
        del index
        while True:
            job = await self._queue.get()
            if job is None:
                self._queue.task_done()
                return
            try:
                if job.cancelled:
                    continue
                self._snapshots[job.handle.handle_id] = RuntimeStatusSnapshot(
                    handle=job.handle,
                    session_id=job.handle.session_id,
                    status="running",
                )
                try:
                    job.result = await self.runtime.run(
                        job.prompt,
                        session_id=job.handle.session_id,
                        title=job.title,
                    )
                except asyncio.CancelledError:
                    self._finish_interrupted_job(job)
                    raise
                except Exception as exc:  # noqa: BLE001 - task isolation boundary.
                    diagnostic = Diagnostic(
                        code="supervised_run_failed",
                        severity="error",
                        component="runtime.supervisor",
                        message=f"runtime task failed ({type(exc).__name__})",
                        session_id=job.handle.session_id,
                        recoverable=False,
                    )
                    self._finish_job(
                        job,
                        RuntimeStatusSnapshot(
                            handle=job.handle,
                            session_id=job.handle.session_id,
                            status="failed",
                            diagnostics=(diagnostic,),
                        ),
                    )
                else:
                    self._finish_job(
                        job,
                        RuntimeStatusSnapshot(
                            handle=job.handle,
                            session_id=job.handle.session_id,
                            run_id=job.result.run.run_id,
                            status=job.result.run.status,
                        ),
                    )
            finally:
                self._queue.task_done()

    def _on_event(self, event: EventRecord) -> None:
        job = self._jobs_by_session.get(event.session_id)
        if job is not None and not job.done.is_set():
            current = self._snapshots[job.handle.handle_id]
            status = _status_from_event(event, current.status)
            self._snapshots[job.handle.handle_id] = current.model_copy(
                update={
                    "run_id": event.run_id,
                    "status": status,
                    "updated_at": utc_now(),
                }
            )
        notification = RuntimeNotification(kind="event", event=event)
        for subscription in tuple(self._subscriptions.get(event.session_id, ())):
            subscription._offer(notification)

    def _resolve_job(self, handle_or_session: RunHandle | str) -> _RunJob:
        if isinstance(handle_or_session, RunHandle):
            job = self._jobs_by_handle.get(handle_or_session.handle_id)
        else:
            job = self._jobs_by_handle.get(handle_or_session)
            if job is None:
                job = self._jobs_by_session.get(handle_or_session)
        if job is None:
            raise KeyError(f"unknown supervised run: {handle_or_session}")
        return job

    def _finish_job(self, job: _RunJob, snapshot: RuntimeStatusSnapshot) -> None:
        self._snapshots[job.handle.handle_id] = snapshot
        if self._jobs_by_session.get(job.handle.session_id) is job:
            self._jobs_by_session.pop(job.handle.session_id, None)
        if self._jobs_by_handle.get(job.handle.handle_id) is job:
            self._jobs_by_handle.pop(job.handle.handle_id, None)
        job.done.set()

    def _finish_interrupted_job(self, job: _RunJob) -> None:
        try:
            durable = self.store.load_session(job.handle.session_id, recover=False)
            active = maybe_active_run(durable.runs)
        except Exception:  # noqa: BLE001 - shutdown must still release process state.
            active = None
        self._finish_job(
            job,
            RuntimeStatusSnapshot(
                handle=job.handle,
                session_id=job.handle.session_id,
                run_id=active.run_id if active is not None else None,
                status="recoverable" if active is not None else "failed",
                diagnostics=(
                    Diagnostic(
                        code="supervised_run_interrupted",
                        severity="warning",
                        component="runtime.supervisor",
                        message=(
                            "the process stopped before the durable run reached a terminal state"
                        ),
                        session_id=job.handle.session_id,
                        run_id=active.run_id if active is not None else None,
                        recoverable=active is not None,
                    ),
                ),
            ),
        )


def _status_from_event(
    event: EventRecord,
    current: SupervisorRunStatus,
) -> SupervisorRunStatus:
    if event.event_type == "run_status_changed":
        status = event.payload.get("to_status")
        if status in {
            "queued",
            "running",
            "waiting_user",
            "waiting_approval",
            "completed",
            "aborted",
            "failed",
            "paused",
            "recoverable",
        }:
            return status
    mapping: dict[str, SupervisorRunStatus] = {
        "run_started": "running",
        "approval_requested": "waiting_approval",
        "ask_user_requested": "waiting_user",
        "run_completed": "completed",
        "run_aborted": "aborted",
        "run_failed": "failed",
        "run_paused": "paused",
    }
    return mapping.get(event.event_type, current)


def _title_from_prompt(prompt: str) -> str:
    normalized = " ".join(prompt.split())
    return normalized[:60] if normalized else "New session"
