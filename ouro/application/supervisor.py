from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Final, Protocol

from pydantic import JsonValue

from ouro.application.dto import Diagnostic
from ouro.application.event_models import project_runtime_event
from ouro.application.runs import maybe_active_run
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_activity import (
    project_runtime_activity_state,
    queued_runtime_activity,
    reduce_runtime_activity,
    runtime_activity_changed,
    runtime_activity_for_status,
)
from ouro.application.supervisor_models import (
    RunHandle,
    RuntimeActivitySnapshot,
    RuntimeNotification,
    RuntimeStatusSnapshot,
    ShutdownPolicy,
    SupervisorRunStatus,
    SupervisorStartResult,
)
from ouro.core.agents import AgentWaitResolution
from ouro.core.base import utc_now
from ouro.core.errors import OuroError, SessionBusyError, SessionNotFoundError
from ouro.core.events import EventRecord
from ouro.core.results import RunResult
from ouro.core.session_controls import DelegatedTaskInput, HostSessionInput
from ouro.core.state import RunRecord
from ouro.core.tools import ToolResult

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
    prepared_run: RunRecord | None = None
    session_input: HostSessionInput | DelegatedTaskInput | None = None
    agent_wait_resolution: AgentWaitResolution | None = None
    agent_wait_result: ToolResult | None = None
    agent_wait_tool_call_id: str | None = None
    interaction_kind: str | None = None
    approval_id: str | None = None
    approval_decision: bool | None = None
    approval_reason: str = "User rejected"
    approval_arguments: dict[str, object] | None = None
    user_answer: JsonValue = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: RunResult | None = None
    cancelled: bool = False


class RuntimeSubscription(AsyncIterator[RuntimeNotification]):
    """Bounded non-blocking view of one Session's runtime stream.

    When activity is requested, ``initial_activity`` reflects the same
    subscription boundary used to select replay events. Replayed events remain
    historical; subsequent activity notifications describe live state changes.
    """

    def __init__(
        self,
        supervisor: RuntimeSupervisor,
        session_id: str,
        *,
        max_buffer: int,
        include_activity: bool,
        initial_activity: RuntimeActivitySnapshot | None,
    ) -> None:
        self.session_id = session_id
        self.include_activity = include_activity
        self.initial_activity = initial_activity
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
            self._offer_lagged()

    def _offer_lagged(self) -> None:
        if self._closed:
            return
        with suppress(asyncio.QueueEmpty):
            while True:
                self._queue.get_nowait()
        diagnostic = Diagnostic(
            code="runtime_subscription_lagged",
            severity="warning",
            component="runtime.supervisor",
            message="subscriber buffer overflowed; one or more events were skipped",
            session_id=self.session_id,
            recoverable=True,
            suggested_action="Reload durable events with the paged Session API.",
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
        self._activities: dict[str, RuntimeActivitySnapshot] = {}
        self._subscriptions: dict[str, set[RuntimeSubscription]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self.runtime.add_event_sink(self._on_event)

    async def start(self) -> SupervisorStartResult:
        async with self._lifecycle_lock:
            return await self._start()

    async def _start(self) -> SupervisorStartResult:
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
                snapshot = self.runtime.session_recovery.recover_runtime(session.session_id)
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
        durable = self.store.load_runtime_session(session_id)
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
        self._publish_activity(
            queued_runtime_activity(
                session_id,
                sequence=self.store.latest_event_sequence(session_id),
            )
        )
        self._queue.put_nowait(job)
        return handle

    async def submit_prepared(
        self,
        run: RunRecord,
        session_input: HostSessionInput | DelegatedTaskInput,
    ) -> RunHandle:
        """Schedule one already persisted queued Run without changing its identity."""

        await self.start()
        if run.status != "queued":
            raise ValueError("prepared supervised run must be queued")
        if not session_input.prompt.strip():
            raise ValueError("run prompt cannot be empty")
        session_id = run.session_id
        if session_id in self._jobs_by_session:
            raise SessionBusyError(f"session already has a supervised run: {session_id}")
        durable = self.store.load_runtime_session(session_id)
        active = maybe_active_run(durable.runs)
        if active is None or active.run_id != run.run_id or active.status != "queued":
            raise SessionBusyError(
                f"prepared run is not the Session's queued active run: {run.run_id}"
            )
        handle = RunHandle(session_id=session_id)
        job = _RunJob(
            handle=handle,
            prompt=session_input.prompt,
            title=session_input.title,
            prepared_run=run,
            session_input=session_input,
        )
        self._jobs_by_session[session_id] = job
        self._jobs_by_handle[handle.handle_id] = job
        self._completion_events[handle.handle_id] = job.done
        self._snapshots[handle.handle_id] = RuntimeStatusSnapshot(
            handle=handle,
            session_id=session_id,
            run_id=run.run_id,
            status="queued",
        )
        self._publish_activity(
            queued_runtime_activity(
                session_id,
                sequence=self.store.latest_event_sequence(session_id),
            ).model_copy(update={"run_id": run.run_id})
        )
        self._queue.put_nowait(job)
        return handle

    async def resume_agent_wait(
        self,
        *,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        resolution: AgentWaitResolution,
        result: ToolResult,
    ) -> RunHandle:
        """Schedule continuation of one parked parent Run."""

        await self.start()
        if session_id in self._jobs_by_session:
            raise SessionBusyError(f"session already has a supervised run: {session_id}")
        run = self.store.load_run(session_id, run_id)
        if run.status != "waiting_child":
            raise ValueError(f"run is not waiting for a child: {run.status}")
        handle = RunHandle(session_id=session_id)
        job = _RunJob(
            handle=handle,
            prompt="",
            title=None,
            agent_wait_resolution=resolution,
            agent_wait_result=result,
            agent_wait_tool_call_id=tool_call_id,
        )
        self._enqueue_job(job, run_id=run_id)
        return handle

    async def route_host_input(
        self,
        session_input: HostSessionInput,
        *,
        session_id: str,
    ) -> RunHandle | None:
        """Route synthetic Agent traffic through the supervised Host boundary."""

        await self.start()
        durable = self.store.load_runtime_session(session_id)
        active = maybe_active_run(durable.runs)
        if active is not None:
            await self.runtime.submit_host_input(session_input, session_id=session_id)
            return None
        if session_id in self._jobs_by_session:
            raise SessionBusyError(f"session already has a supervised run: {session_id}")
        handle = RunHandle(session_id=session_id)
        job = _RunJob(
            handle=handle,
            prompt=session_input.prompt,
            title=session_input.title,
            session_input=session_input,
        )
        self._enqueue_job(job)
        return handle

    async def resolve_tool_approval(
        self,
        *,
        session_id: str,
        run_id: str,
        approval_id: str,
        approved: bool,
        reason: str = "User rejected",
        arguments_override: dict[str, object] | None = None,
    ) -> RunHandle:
        await self.start()
        await self._await_parked_session(
            session_id,
            run_id=run_id,
            expected_status="waiting_approval",
        )
        run = self.store.load_run(session_id, run_id)
        if run.status != "waiting_approval":
            raise ValueError(f"run is not waiting for approval: {run.status}")
        handle = RunHandle(session_id=session_id)
        self._enqueue_job(
            _RunJob(
                handle=handle,
                prompt="",
                title=None,
                interaction_kind="approval",
                approval_id=approval_id,
                approval_decision=approved,
                approval_reason=reason,
                approval_arguments=arguments_override,
            ),
            run_id=run_id,
        )
        return handle

    async def resolve_tool_input(
        self,
        *,
        session_id: str,
        run_id: str,
        answer: JsonValue,
    ) -> RunHandle:
        await self.start()
        await self._await_parked_session(
            session_id,
            run_id=run_id,
            expected_status="waiting_user",
        )
        run = self.store.load_run(session_id, run_id)
        if run.status != "waiting_user":
            raise ValueError(f"run is not waiting for user input: {run.status}")
        handle = RunHandle(session_id=session_id)
        self._enqueue_job(
            _RunJob(
                handle=handle,
                prompt="",
                title=None,
                interaction_kind="user_input",
                user_answer=answer,
            ),
            run_id=run_id,
        )
        return handle

    async def _await_parked_session(
        self,
        session_id: str,
        *,
        run_id: str,
        expected_status: str,
    ) -> None:
        job = self._jobs_by_session.get(session_id)
        if job is None:
            return
        run = self.store.load_run(session_id, run_id)
        if run.status != expected_status:
            raise SessionBusyError(f"session already has a supervised run: {session_id}")
        await job.done.wait()

    def _enqueue_job(self, job: _RunJob, *, run_id: str | None = None) -> None:
        session_id = job.handle.session_id
        self._jobs_by_session[session_id] = job
        self._jobs_by_handle[job.handle.handle_id] = job
        self._completion_events[job.handle.handle_id] = job.done
        self._snapshots[job.handle.handle_id] = RuntimeStatusSnapshot(
            handle=job.handle,
            session_id=session_id,
            run_id=run_id,
            status="queued",
        )
        activity = queued_runtime_activity(
            session_id,
            sequence=self.store.latest_event_sequence(session_id),
        )
        self._publish_activity(
            activity if run_id is None else activity.model_copy(update={"run_id": run_id})
        )
        self._queue.put_nowait(job)

    def subscribe(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        max_buffer: int = 256,
        include_activity: bool = False,
    ) -> RuntimeSubscription:
        if self._closed:
            raise OuroError("runtime supervisor is closed")
        if max_buffer <= 0:
            raise ValueError("max_buffer must be positive")
        try:
            self.store.load_session_record(session_id)
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc
        activity = self._activities.get(session_id)
        if activity is None:
            active_job = self._jobs_by_session.get(session_id)
            status_override = (
                self._snapshots[active_job.handle.handle_id].status
                if active_job is not None
                else None
            )
            activity = self._load_activity(session_id, status_override=status_override)
            self._activities[session_id] = activity
        subscription = RuntimeSubscription(
            self,
            session_id,
            max_buffer=max_buffer,
            include_activity=include_activity,
            initial_activity=activity if include_activity else None,
        )
        self._subscriptions.setdefault(session_id, set()).add(subscription)
        events, truncated = self.store.load_events_after(
            session_id,
            after_sequence=after_sequence,
            limit=max_buffer,
        )
        if truncated:
            subscription._offer_lagged()
        else:
            for event in events:
                subscription._offer(
                    RuntimeNotification(kind="event", event=project_runtime_event(event))
                )
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
            durable_snapshot = self.store.load_runtime_session(handle_or_session)
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
            status=(run.status if run is not None else "idle"),
        )

    def activity(self, handle_or_session: RunHandle | str) -> RuntimeActivitySnapshot:
        session_id = self._activity_session_id(handle_or_session)
        is_handle_lookup = isinstance(handle_or_session, RunHandle) or (
            isinstance(handle_or_session, str)
            and (handle_or_session in self._jobs_by_handle or handle_or_session in self._snapshots)
        )
        requested_status = self.status(handle_or_session)
        cached = self._activities.get(session_id)
        if cached is not None and (
            requested_status.run_id is None or cached.run_id == requested_status.run_id
        ):
            return cached
        try:
            activity = self._load_activity(
                session_id,
                run_id=requested_status.run_id,
                status_override=requested_status.status,
            )
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc
        if not is_handle_lookup and requested_status.run_id == activity.run_id:
            self._activities[session_id] = activity
        return activity

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
            durable = self.status(handle_or_session)
            if durable.run_id is None or durable.status not in {
                "queued",
                "running",
                "waiting_user",
                "waiting_approval",
                "waiting_child",
            }:
                return durable
            result = await self.runtime.abort_run(
                session_id=handle_or_session,
                run_id=durable.run_id,
                reason=reason,
            )
            return RuntimeStatusSnapshot(
                session_id=handle_or_session,
                run_id=result.run.run_id,
                status=result.run.status,
            )
        snapshot = self._snapshots[job.handle.handle_id]
        if snapshot.status == "queued":
            job.cancelled = True
            if snapshot.run_id is not None:
                await self.runtime.abort_run(
                    session_id=job.handle.session_id,
                    run_id=snapshot.run_id,
                    reason=reason,
                )
            self._finish_job(
                job,
                RuntimeStatusSnapshot(
                    handle=job.handle,
                    session_id=job.handle.session_id,
                    run_id=snapshot.run_id,
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
        if snapshot.status in {"running", "waiting_user", "waiting_approval", "waiting_child"}:
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
        async with self._lifecycle_lock:
            await self._shutdown(policy)

    async def _shutdown(self, policy: ShutdownPolicy | None = None) -> None:
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
            self.runtime.close(),
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
                queued_snapshot = self._snapshots[job.handle.handle_id]
                self._snapshots[job.handle.handle_id] = queued_snapshot.model_copy(
                    update={"status": "running", "updated_at": utc_now()}
                )
                current_activity = self._activities.get(job.handle.session_id)
                if current_activity is None:
                    current_activity = queued_runtime_activity(job.handle.session_id)
                self._publish_activity(
                    current_activity.model_copy(
                        update={
                            "status": "running",
                            "activity": "preparing_model",
                            "updated_at": utc_now(),
                        }
                    )
                )
                try:
                    if job.interaction_kind == "approval":
                        run_id = self._snapshots[job.handle.handle_id].run_id
                        assert run_id is not None
                        assert job.approval_id is not None
                        assert job.approval_decision is not None
                        job.result = await self.runtime.approve_tool(
                            session_id=job.handle.session_id,
                            run_id=run_id,
                            approval_id=job.approval_id,
                            approved=job.approval_decision,
                            reason=job.approval_reason,
                            arguments_override=job.approval_arguments,
                        )
                    elif job.interaction_kind == "user_input":
                        run_id = self._snapshots[job.handle.handle_id].run_id
                        assert run_id is not None
                        job.result = await self.runtime.answer_user(
                            session_id=job.handle.session_id,
                            run_id=run_id,
                            answer=job.user_answer,
                        )
                    elif job.agent_wait_resolution is not None:
                        assert job.agent_wait_result is not None
                        assert job.agent_wait_tool_call_id is not None
                        run_id = self._snapshots[job.handle.handle_id].run_id
                        assert run_id is not None
                        job.result = await self.runtime.resume_agent_wait(
                            session_id=job.handle.session_id,
                            run_id=run_id,
                            tool_call_id=job.agent_wait_tool_call_id,
                            resolution=job.agent_wait_resolution,
                            result=job.agent_wait_result,
                        )
                    elif job.prepared_run is not None:
                        assert job.session_input is not None
                        job.result = await self.runtime.run_prepared(
                            job.prepared_run,
                            job.session_input,
                        )
                    elif job.session_input is not None:
                        assert isinstance(job.session_input, HostSessionInput)
                        job.result = await self.runtime.submit_host_input(
                            job.session_input,
                            session_id=job.handle.session_id,
                        )
                    else:
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

    async def _on_event(self, event: EventRecord) -> None:
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
        subscriptions = tuple(self._subscriptions.get(event.session_id, ()))
        notification = RuntimeNotification(kind="event", event=project_runtime_event(event))
        for subscription in subscriptions:
            subscription._offer(notification)
        previous_activity = self._activities.get(event.session_id)
        if previous_activity is None:
            current_activity = self._load_activity(event.session_id)
        else:
            current_activity = reduce_runtime_activity(previous_activity, event)
        self._activities[event.session_id] = current_activity
        if previous_activity is None or runtime_activity_changed(
            previous_activity,
            current_activity,
        ):
            activity_notification = RuntimeNotification(
                kind="activity",
                activity=current_activity,
            )
            for subscription in subscriptions:
                if subscription.include_activity:
                    subscription._offer(activity_notification)

    def _load_activity(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        status_override: SupervisorRunStatus | None = None,
    ) -> RuntimeActivitySnapshot:
        session = self.store.load_session_record(session_id)
        selected_run_id = run_id or session.active_run_id or session.last_run_id
        run = (
            self.store.load_run(session_id, selected_run_id)
            if selected_run_id is not None
            else None
        )
        return project_runtime_activity_state(
            session,
            run,
            sequence=self.store.latest_event_sequence(session_id),
            status_override=status_override,
            context_status=self.runtime.sessions.context_status(session_id),
        )

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
        current_activity = self._activities.get(job.handle.session_id)
        if current_activity is None:
            current_activity = RuntimeActivitySnapshot(
                session_id=job.handle.session_id,
                run_id=snapshot.run_id,
                status=snapshot.status,
                activity=runtime_activity_for_status(snapshot.status),
            )
        elif current_activity.status != snapshot.status:
            current_activity = current_activity.model_copy(
                update={
                    "run_id": snapshot.run_id or current_activity.run_id,
                    "status": snapshot.status,
                    "activity": runtime_activity_for_status(snapshot.status),
                    "updated_at": utc_now(),
                }
            )
        self._publish_activity(current_activity)
        if self._jobs_by_session.get(job.handle.session_id) is job:
            self._jobs_by_session.pop(job.handle.session_id, None)
        if self._jobs_by_handle.get(job.handle.handle_id) is job:
            self._jobs_by_handle.pop(job.handle.handle_id, None)
        job.done.set()

    def _publish_activity(self, activity: RuntimeActivitySnapshot) -> None:
        previous = self._activities.get(activity.session_id)
        self._activities[activity.session_id] = activity
        if previous is not None and not runtime_activity_changed(previous, activity):
            return
        notification = RuntimeNotification(kind="activity", activity=activity)
        for subscription in tuple(self._subscriptions.get(activity.session_id, ())):
            if subscription.include_activity:
                subscription._offer(notification)

    def _activity_session_id(self, handle_or_session: RunHandle | str) -> str:
        if isinstance(handle_or_session, RunHandle):
            return handle_or_session.session_id
        job = self._jobs_by_handle.get(handle_or_session)
        if job is not None:
            return job.handle.session_id
        snapshot = self._snapshots.get(handle_or_session)
        if snapshot is not None:
            return snapshot.session_id
        return handle_or_session

    def _finish_interrupted_job(self, job: _RunJob) -> None:
        try:
            durable = self.store.load_runtime_session(job.handle.session_id)
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
        status = event.payload.get("to_status", event.payload.get("status"))
        if status in {
            "queued",
            "running",
            "waiting_user",
            "waiting_approval",
            "waiting_child",
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
