from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from typing import Final, Protocol

from pydantic import JsonValue

from ouro.application.contracts.base import Diagnostic
from ouro.application.contracts.events import project_runtime_event
from ouro.application.contracts.execution import (
    HostInputReceipt,
    RunHandle,
    RuntimeActivitySnapshot,
    RuntimeNotification,
    RuntimeStatusSnapshot,
    ShutdownPolicy,
    SupervisorRunStatus,
    SupervisorStartResult,
)
from ouro.application.contracts.sessions import SessionHistoryAnchor
from ouro.application.inputs.requests import UserInputRequest, UserInputSubmission
from ouro.application.observations.activity import (
    project_runtime_activity_state,
    queued_runtime_activity,
    reduce_runtime_activity,
    runtime_activity_changed,
    runtime_activity_for_status,
)
from ouro.application.observations.events import SessionEventStream
from ouro.application.observations.sessions import SessionObservation, redact_observation
from ouro.application.observations.subscription import SessionStateSubscription
from ouro.application.runs.lifecycle import maybe_active_run
from ouro.application.runs.ownership import RunReservation
from ouro.application.runtime.service import RuntimeService
from ouro.application.sessions.editing import continuable_run
from ouro.core.agents import AgentWaitResolution
from ouro.core.base import utc_now
from ouro.core.callbacks import run_blocking
from ouro.core.errors import OuroError, PersistenceError, SessionBusyError, SessionNotFoundError
from ouro.core.events import EventRecord
from ouro.core.results import RunResult
from ouro.core.session_controls import DelegatedTaskInput, HostSessionInput
from ouro.core.state import (
    ACTIVE_RUN_STATUSES,
    RESUMABLE_RUN_STATUSES,
    RunRecord,
    RunStatus,
    SessionRecord,
)
from ouro.core.tools import ToolResult

_CLOSED: Final = object()
_LOGGER = logging.getLogger(__name__)


class RuntimeManagedResource(Protocol):
    """A long-lived process resource owned by RuntimeSupervisor."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...


@dataclass
class _RunJob:
    handle: RunHandle
    prompt: str
    title: str | None
    background: bool = False
    prepared_run: RunRecord | None = None
    reservation: RunReservation | None = None
    continue_existing: bool = False
    history_epoch: int | None = None
    session_input: HostSessionInput | DelegatedTaskInput | None = None
    host_input_receipt: HostInputReceipt | None = None
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
    error: Exception | None = None
    detached: bool = False
    cancelled: bool = False
    start_after: asyncio.Event | None = None
    provider_stream_started: asyncio.Event = field(default_factory=asyncio.Event)
    run_identified: asyncio.Event = field(default_factory=asyncio.Event)
    execution_started: asyncio.Event = field(default_factory=asyncio.Event)


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
        after_sequence: int = 0,
    ) -> None:
        self.session_id = session_id
        self.include_activity = include_activity
        self.initial_activity = initial_activity
        self._supervisor = supervisor
        self._queue: asyncio.Queue[RuntimeNotification | object] = asyncio.Queue(max_buffer)
        self._closed = False
        self._event_sequence = after_sequence

    def __aiter__(self) -> RuntimeSubscription:
        return self

    async def __anext__(self) -> RuntimeNotification:
        if self._closed and self._queue.empty():
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        assert isinstance(item, RuntimeNotification)
        return item

    async def get(self) -> RuntimeNotification:
        return await self.__anext__()

    def get_nowait(self) -> RuntimeNotification:
        if self._closed and self._queue.empty():
            raise StopAsyncIteration
        item = self._queue.get_nowait()
        if item is _CLOSED:
            raise StopAsyncIteration
        assert isinstance(item, RuntimeNotification)
        return item

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._supervisor.unsubscribe(self)
        self._offer_closed()

    def _offer(self, notification: RuntimeNotification) -> None:
        if self._closed:
            return
        if notification.event is not None:
            if notification.event.sequence <= self._event_sequence:
                return
            self._event_sequence = notification.event.sequence
        try:
            self._queue.put_nowait(notification)
        except asyncio.QueueFull:
            with suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self._offer_lagged()

    def fail(self, diagnostic: Diagnostic) -> None:
        if self._closed:
            return
        self._closed = True
        self._supervisor.unsubscribe(self)
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(RuntimeNotification(kind="diagnostic", diagnostic=diagnostic))

    def _prepend_replay(
        self,
        replay: tuple[RuntimeNotification, ...],
        *,
        after_sequence: int,
        truncated: bool,
    ) -> None:
        """Merge replay and live arrivals before exposing this subscription."""

        pending: list[RuntimeNotification] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if isinstance(item, RuntimeNotification):
                pending.append(item)
        for item in pending:
            if (
                item.diagnostic is not None
                and item.diagnostic.code != "runtime_subscription_lagged"
            ):
                # A failed live reader cannot be repaired by replay merging.
                # Preserve its cause instead of asking hosts to reload in a loop.
                self._offer(item)
                return
        if truncated or any(item.kind == "diagnostic" for item in pending):
            self._offer_lagged()
            return
        self._event_sequence = after_sequence
        for notification in (*replay, *pending):
            if (
                notification.activity is not None
                and notification.activity.sequence < self._event_sequence
            ):
                # Replay already crossed this activity's durable boundary.
                # Do not move a consumer back to an older live snapshot.
                continue
            self._offer(notification)

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
        self._state_subscriptions: dict[str, set[SessionStateSubscription]] = {}
        self._event_streams: dict[str, SessionEventStream] = {}
        self._event_stream_tasks: set[asyncio.Task[None]] = set()
        self._parked_session_locks: dict[str, asyncio.Lock] = {}
        self._pending_host_inputs: dict[str, HostInputReceipt] = {}
        self._host_dispatch_tasks: set[asyncio.Task[None]] = set()
        self._input_admissions: set[asyncio.Task[UserInputSubmission]] = set()
        self._latest_job_start = asyncio.Event()
        self._latest_job_start.set()
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self.runtime.add_event_sink(self._on_event)

    async def start(self) -> SupervisorStartResult:
        if self._started:
            return SupervisorStartResult()
        async with self._lifecycle_lock:
            return await self._start()

    async def recover_session(self, session_id: str) -> None:
        """Recover one Session execution boundary without scanning the store."""

        await self.start()
        # An existing execution owner excludes recovery. Checking that boundary
        # needs no message history; interrupted Sessions still take the full path.
        if await run_blocking(self.runtime.run_ownership.is_locked, session_id):
            return
        await self.runtime.reconcile_execution_session(session_id)

    async def _start(self) -> SupervisorStartResult:
        if self._closed:
            raise OuroError("runtime supervisor is closed")
        if self._started:
            return SupervisorStartResult()
        try:
            for resource in self._managed_resources:
                await resource.start()
                self._started_resources.append(resource)
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
        return SupervisorStartResult()

    async def submit_user_input(
        self,
        request: UserInputRequest,
        *,
        on_session_created: Callable[[SessionRecord], None] | None = None,
        on_input_accepted: Callable[[], None] | None = None,
    ) -> UserInputSubmission:
        """Hand off input admission; cancelling the caller does not retract accepted work."""
        async with self._lifecycle_lock:
            await self._start()
            task = asyncio.create_task(
                self._admit_user_input(request, on_session_created, on_input_accepted),
                name=f"ouro-input-{request.input_id}",
            )
            self._input_admissions.add(task)
            task.add_done_callback(self._input_admission_finished)
        return await asyncio.shield(task)

    def _input_admission_finished(self, task: asyncio.Task[UserInputSubmission]) -> None:
        self._input_admissions.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("User input admission failed: %s", type(task.exception()).__name__)

    async def _admit_user_input(
        self,
        request: UserInputRequest,
        on_session_created: Callable[[SessionRecord], None] | None,
        on_input_accepted: Callable[[], None] | None,
    ) -> UserInputSubmission:
        receipt = await self.runtime.accept_user_input(request)
        active = await run_blocking(self._load_active_run, receipt.session_id)
        # Notify before execution can publish events for a newly selected Session.
        try:
            if request.session_id is None and on_session_created is not None:
                session = await run_blocking(self.store.load_session_record, receipt.session_id)
                on_session_created(session)
            if on_input_accepted is not None:
                on_input_accepted()
        finally:
            # A Host callback cannot strand a durable accepted input.
            self._pending_host_inputs[receipt.session_id] = receipt
            job = await self._dispatch_host_input(receipt.session_id)

        async def result() -> RunResult:
            if receipt.run_id is not None:
                return await run_blocking(
                    self.runtime.sessions.run_result, receipt.session_id, receipt.run_id
                )
            if active is not None and request.session_id is not None:
                return await run_blocking(
                    self.runtime.sessions.run_result, receipt.session_id, active.run_id
                )
            if job is not None:
                await job.done.wait()
                if job.cancelled and job.result is None:
                    raise asyncio.CancelledError("Accepted input was cancelled before execution")
                if job.error is not None:
                    raise job.error
                if job.result is not None:
                    return job.result
            return await run_blocking(self._accepted_input_result, receipt)

        return UserInputSubmission(receipt=receipt, _result=result)

    def _accepted_input_result(self, receipt: HostInputReceipt) -> RunResult:
        with self.store.session_read(receipt.session_id):
            message = self.store.find_queued_input_message(receipt.session_id, receipt.queue_id)
            if message is None or message.run_id is None:
                raise PersistenceError("Accepted input has no execution result")
            return self.runtime.sessions.run_result(receipt.session_id, message.run_id)

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
            created = await run_blocking(
                lambda: self.runtime.sessions.create(
                    title=title,
                    current_model=self.runtime.current_model().key,
                )
            )
            session_id = created.session.session_id
        else:
            try:
                await run_blocking(self.store.load_session_record, session_id)
            except FileNotFoundError as exc:
                raise SessionNotFoundError(f"session not found: {session_id}") from exc
            await self.runtime.reconcile_session(session_id)
        if session_id in self._jobs_by_session:
            raise SessionBusyError(f"session already has a supervised run: {session_id}")
        if await run_blocking(self._load_active_run, session_id) is not None:
            raise SessionBusyError(f"session already has an active run: {session_id}")

        handle = RunHandle(session_id=session_id)
        job = _RunJob(handle=handle, prompt=prompt, title=title)
        await self._enqueue_job(job)
        return handle

    async def continue_run(self, *, session_id: str, run_id: str | None = None) -> RunHandle:
        """Schedule a continuation against the history accepted by this Host request."""
        await self.start()
        previous_job = self._jobs_by_session.get(session_id)
        if previous_job is not None:
            previous_id = self._snapshots[previous_job.handle.handle_id].run_id
            if (
                previous_id is None
                or not previous_job.execution_started.is_set()
                or (run_id is not None and previous_id != run_id)
            ):
                raise SessionBusyError(f"session already has a supervised run: {session_id}")
            previous_run = await run_blocking(self.store.load_run, session_id, previous_id)
            if previous_run.status not in RESUMABLE_RUN_STATUSES:
                raise SessionBusyError(f"session already has a supervised run: {session_id}")
            # Durable interruption is observable before the attempt releases its
            # local slot and reservation. Resume only after that ownership settles.
            await previous_job.done.wait()
        snapshot = await run_blocking(self.runtime.session_recovery.recover_runtime, session_id)
        run = continuable_run(snapshot)
        if run_id is not None and run.run_id != run_id:
            raise SessionBusyError("The requested continuation is no longer the latest run")
        handle = RunHandle(session_id=session_id)
        await self._enqueue_job(
            _RunJob(
                handle=handle,
                prompt="",
                title=None,
                continue_existing=True,
                history_epoch=snapshot.session.history_epoch,
            ),
            run_id=run.run_id,
        )
        return handle

    async def submit_prepared(
        self,
        run: RunRecord,
        session_input: HostSessionInput | DelegatedTaskInput,
        *,
        background: bool = False,
    ) -> RunHandle:
        """Schedule one already persisted queued Run without changing its identity."""

        await self.start()
        if run.status != "queued":
            raise ValueError("prepared supervised run must be queued")
        if not session_input.prompt.strip():
            raise ValueError("run prompt cannot be empty")
        session_id = run.session_id
        previous_job = self._jobs_by_session.get(session_id)
        if previous_job is not None:
            previous_id = self._snapshots[previous_job.handle.handle_id].run_id
            if previous_id is None or previous_id == run.run_id:
                raise SessionBusyError(f"session already has a supervised run: {session_id}")
            previous_run = await run_blocking(self.store.load_run, session_id, previous_id)
            if previous_run.status in ACTIVE_RUN_STATUSES:
                raise SessionBusyError(f"session already has a supervised run: {session_id}")
            # A durable terminal result can be observed before its local job
            # releases ownership. Admit the queued successor after that release.
            await previous_job.done.wait()
        active = await run_blocking(self._load_active_run, session_id)
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
            background=background,
        )
        await self._enqueue_job(job, run_id=run.run_id)
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
        lock = self._parked_session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await self._await_parked_session(
                session_id,
                run_id=run_id,
                expected_status="waiting_child",
            )
            run = await run_blocking(self.store.load_run, session_id, run_id)
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
            await self._enqueue_job(job, run_id=run_id)
            return handle

    async def route_host_input(
        self,
        session_input: HostSessionInput,
        *,
        session_id: str,
    ) -> HostInputReceipt:
        """Durably accept scaffold Host traffic and schedule idle consumption."""

        await self.start()
        receipt = await self.runtime.submit_host_input(session_input, session_id=session_id)
        if receipt.message_id is not None:
            return receipt
        self._pending_host_inputs[session_id] = receipt
        await self._dispatch_host_input(session_id)
        return receipt

    async def _dispatch_host_input(self, session_id: str) -> _RunJob | None:
        lock = self._parked_session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            receipt = self._pending_host_inputs.get(session_id)
            if receipt is None or self._closed:
                return None
            if session_id in self._jobs_by_session:
                # A job may have passed its last queue drain. Its completion
                # must reconsider this receipt after releasing the local slot.
                return self._jobs_by_session[session_id]
            status = await run_blocking(self.status, session_id)
            if status.status == "running" and not await run_blocking(
                self.runtime.run_ownership.is_locked, session_id
            ):
                await self.runtime.reconcile_execution_session(session_id)
                status = await run_blocking(self.status, session_id)
            job = None
            if status.status not in ACTIVE_RUN_STATUSES:
                session = await run_blocking(self.store.load_session_record, session_id)
                if self.runtime.queued_inputs.find_session(session, receipt.queue_id) is not None:
                    job = _RunJob(
                        handle=RunHandle(session_id=session_id),
                        prompt="",
                        title=None,
                        host_input_receipt=receipt,
                    )
                    await self._enqueue_job(job)
            if self._pending_host_inputs.get(session_id) is receipt:
                self._pending_host_inputs.pop(session_id, None)
            return job

    async def _dispatch_after_job(self, session_id: str) -> None:
        for attempt, delay in enumerate((0, 0.1, 0.5, 2.0)):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._dispatch_host_input(session_id)
                return
            except Exception as exc:
                if isinstance(exc, (OSError, PersistenceError, SessionBusyError)) and attempt < 3:
                    continue
                await self.runtime.event_recorder.publish(
                    session_id=session_id,
                    event_type="diagnostic",
                    run_id=None,
                    payload={
                        "code": "host_input_dispatch_failed",
                        "severity": "error",
                        "component": "runtime.supervisor",
                        "error_type": type(exc).__name__,
                        "recoverable": True,
                    },
                )
                return

    def _host_dispatch_finished(self, task: asyncio.Task[None]) -> None:
        self._host_dispatch_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error(
                "Host input dispatch reporting failed: %s", type(task.exception()).__name__
            )

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
        run = await run_blocking(self.store.load_run, session_id, run_id)
        if run.status != "waiting_approval":
            raise ValueError(f"run is not waiting for approval: {run.status}")
        handle = RunHandle(session_id=session_id)
        await self._enqueue_job(
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
        run = await run_blocking(self.store.load_run, session_id, run_id)
        if run.status != "waiting_user":
            raise ValueError(f"run is not waiting for user input: {run.status}")
        handle = RunHandle(session_id=session_id)
        await self._enqueue_job(
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
        run = await run_blocking(self.store.load_run, session_id, run_id)
        if run.status != expected_status:
            raise SessionBusyError(f"session already has a supervised run: {session_id}")
        await job.done.wait()

    async def _enqueue_job(self, job: _RunJob, *, run_id: str | None = None) -> None:
        session_id = job.handle.session_id

        def read_boundary() -> tuple[int, int]:
            with self.store.session_read(session_id):
                if job.prepared_run is not None:
                    active = self._load_active_run(session_id)
                    if (
                        active is None
                        or active.run_id != job.prepared_run.run_id
                        or active.status != "queued"
                    ):
                        raise SessionBusyError("prepared Run is no longer queued")
                return (
                    self.store.load_session_record(session_id).history_epoch,
                    self.store.latest_event_sequence(session_id),
                )

        if job.prepared_run is not None:
            job.reservation = await self.runtime.run_ownership.reserve(
                session_id, job.prepared_run.run_id
            )
            if job.reservation is None:
                raise SessionBusyError(f"session already has an execution owner: {session_id}")
        try:
            epoch, sequence = await run_blocking(read_boundary)
            # Admission publishes only after the durable queued Run has a real
            # execution owner. All Hosts' recovery paths observe the same lock.
            if self._closed:
                raise OuroError("runtime supervisor is closed")
            if session_id in self._jobs_by_session:
                raise SessionBusyError(f"session already has a supervised run: {session_id}")
        except BaseException:
            if job.reservation is not None:
                await run_blocking(job.reservation.release)
            raise
        if job.history_epoch is None:
            job.history_epoch = epoch
        # Queries yield to other submissions and shutdown. Commit process-local
        # scheduling atomically only after checking the current ownership again.
        if not job.background:
            job.start_after = self._latest_job_start
            self._latest_job_start = job.provider_stream_started
        self._jobs_by_session[session_id] = job
        self._jobs_by_handle[job.handle.handle_id] = job
        self._completion_events[job.handle.handle_id] = job.done
        self._snapshots[job.handle.handle_id] = RuntimeStatusSnapshot(
            handle=job.handle,
            session_id=session_id,
            run_id=run_id,
            status="queued",
        )
        if run_id is None or job.prepared_run is not None:
            activity = queued_runtime_activity(
                session_id, sequence=sequence, history_epoch=job.history_epoch
            )
            self._publish_activity(
                activity if run_id is None else activity.model_copy(update={"run_id": run_id})
            )
        self._queue.put_nowait(job)

    async def observe_session(
        self,
        session_id: str,
        *,
        page_size: int = 128,
        max_buffer: int = 256,
        history_limit: int | None = None,
        history_anchor: SessionHistoryAnchor | None = None,
    ) -> SessionObservation:
        """Observe display facts without taking over or recovering execution."""
        if page_size <= 0 or max_buffer <= 0:
            raise ValueError("Observation page size and buffer size must be positive")
        read_snapshot = self.runtime.observation_snapshot
        if history_limit is not None:
            read_snapshot = partial(read_snapshot, history_limit=history_limit)
        snapshot = (
            await run_blocking(read_snapshot, session_id, history_anchor=history_anchor)
            if history_anchor is not None
            else await run_blocking(read_snapshot, session_id)
        )
        source = await self.subscribe(
            session_id, after_sequence=snapshot.event_high_water, max_buffer=max_buffer
        )
        try:
            snapshot = await run_blocking(redact_observation, snapshot)
            return SessionObservation(
                snapshot,
                source,
                sessions=self.runtime.sessions,
                read_snapshot=read_snapshot,
                page_size=page_size,
                history_anchor=history_anchor,
            )
        except BaseException:
            source.close()
            raise

    async def subscribe(
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
        subscription = RuntimeSubscription(
            self,
            session_id,
            max_buffer=max_buffer,
            include_activity=include_activity,
            initial_activity=None,
            after_sequence=after_sequence,
        )
        # Attach before the read yields: every event is covered by durable
        # replay, the bounded live queue, or both. Neither producer is paused.
        self._subscriptions.setdefault(session_id, set()).add(subscription)
        try:
            initial = await self._observe_session_state(session_id)
            if include_activity:
                subscription.initial_activity = initial

            def read_replay() -> tuple[tuple[RuntimeNotification, ...], bool]:
                # Events are immutable facts; read replay outside the snapshot lock.
                events, truncated = self.store.load_events_after(
                    session_id,
                    after_sequence=after_sequence,
                    limit=max_buffer,
                )
                replay = (
                    tuple(
                        RuntimeNotification(kind="event", event=project_runtime_event(event))
                        for event in events
                        if event.history_epoch >= initial.history_epoch
                    )
                    if not truncated
                    else ()
                )
                return replay, truncated

            replay, truncated = await run_blocking(read_replay)
            if self._closed:
                raise OuroError("runtime supervisor is closed")
            if not subscription.closed:
                subscription._prepend_replay(
                    replay, after_sequence=after_sequence, truncated=truncated
                )
        except BaseException as exc:
            subscription.close()
            if isinstance(exc, FileNotFoundError):
                raise SessionNotFoundError(f"session not found: {session_id}") from exc
            raise
        return subscription

    async def _observe_session_state(self, session_id: str) -> RuntimeActivitySnapshot:
        active_job = self._jobs_by_session.get(session_id)
        status_override = (
            self._snapshots[active_job.handle.handle_id].status if active_job is not None else None
        )
        cached = self._activities.get(session_id)
        initial = (
            cached
            if session_id in self._event_streams
            or (active_job is not None and cached is not None and cached.run_id is None)
            else None
        )
        if initial is None:
            initial = await run_blocking(
                self._load_activity, session_id, status_override=status_override
            )
        if self._closed or not (
            self._subscriptions.get(session_id) or self._state_subscriptions.get(session_id)
        ):
            raise OuroError("runtime supervisor is closed")
        current = self._activities.get(session_id)
        if current is None or current.sequence <= initial.sequence:
            self._activities[session_id] = initial
        if session_id not in self._event_streams:
            # Seed the reducer before following this exact snapshot cursor.
            # A concurrent writer may commit while the read transaction is
            # open; those events must update activity as well as reach Hosts.
            stream = SessionEventStream(
                self.store,
                session_id,
                after_sequence=initial.sequence,
                publish=self._publish_observed_event,
                report=self._report_observation_error,
                is_running=lambda: (
                    session_id in self._jobs_by_session
                    or (
                        (current := self._activities.get(session_id)) is not None
                        and current.status in {"queued", "running"}
                    )
                ),
            )
            self._event_streams[session_id] = stream
            task = asyncio.create_task(stream.follow(), name=f"ouro-session-events-{session_id}")
            self._event_stream_tasks.add(task)
            task.add_done_callback(self._event_stream_tasks.discard)

        return initial

    async def subscribe_state(
        self, session_id: str, *, event_types: frozenset[str] = frozenset()
    ) -> SessionStateSubscription:
        """Watch activity and selected relation changes without buffering text history."""
        if self._closed:
            raise OuroError("runtime supervisor is closed")
        subscription = SessionStateSubscription(
            session_id, event_types=event_types, on_close=self._unsubscribe_state
        )
        self._state_subscriptions.setdefault(session_id, set()).add(subscription)
        try:
            subscription.initial_activity = await self._observe_session_state(session_id)
        except BaseException as exc:
            subscription.close()
            if isinstance(exc, FileNotFoundError):
                raise SessionNotFoundError(f"session not found: {session_id}") from exc
            raise
        return subscription

    def unsubscribe(self, subscription: RuntimeSubscription) -> None:
        subscriptions = self._subscriptions.get(subscription.session_id)
        if subscriptions is not None:
            subscriptions.discard(subscription)
            if not subscriptions:
                self._subscriptions.pop(subscription.session_id, None)
        self._release_session_stream(subscription.session_id)

    def _unsubscribe_state(self, subscription: SessionStateSubscription) -> None:
        subscriptions = self._state_subscriptions.get(subscription.session_id)
        if subscriptions is not None:
            subscriptions.discard(subscription)
            if not subscriptions:
                self._state_subscriptions.pop(subscription.session_id, None)
        self._release_session_stream(subscription.session_id)

    def _release_session_stream(self, session_id: str) -> None:
        if self._subscriptions.get(session_id) or self._state_subscriptions.get(session_id):
            return
        stream = self._event_streams.pop(session_id, None)
        if stream is not None:
            stream.close()
        if session_id not in self._jobs_by_session:
            self._activities.pop(session_id, None)

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
            pending = self._snapshots[active.handle.handle_id]
            if pending.run_id is None:
                return pending
        try:
            with self.store.session_read(handle_or_session):
                session = self.store.load_session_record(handle_or_session)
                run_id = session.active_run_id or session.last_run_id
                run = self.store.load_run(handle_or_session, run_id) if run_id is not None else None
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {handle_or_session}") from exc
        return RuntimeStatusSnapshot(
            session_id=handle_or_session,
            run_id=run.run_id if run is not None else None,
            status=(run.status if run is not None else "idle"),
        )

    def activity(self, handle_or_session: RunHandle | str) -> RuntimeActivitySnapshot:
        session_id = self._activity_session_id(handle_or_session)
        requested_status = (
            self.status(handle_or_session)
            if isinstance(handle_or_session, RunHandle) or handle_or_session in self._snapshots
            else None
        )
        cached = self._activities.get(session_id)
        if (
            cached is not None
            and (session_id in self._event_streams or cached.run_id is None)
            and (requested_status is None or cached.run_id == requested_status.run_id)
        ):
            return cached
        try:
            activity = self._load_activity(
                session_id,
                run_id=requested_status.run_id if requested_status else None,
                status_override=requested_status.status if requested_status else None,
            )
        except FileNotFoundError as exc:
            raise SessionNotFoundError(f"session not found: {session_id}") from exc
        return activity

    async def wait(self, handle: RunHandle) -> RuntimeStatusSnapshot:
        done = self._completion_events.get(handle.handle_id)
        if done is None:
            raise KeyError(f"unknown run handle: {handle.handle_id}")
        await done.wait()
        return self.status(handle)

    async def wait_run(
        self, session_id: str, run_id: str, *, until: frozenset[RunStatus] = frozenset()
    ) -> RunRecord:
        """Wait for a durable Run, including execution owned by another Host.

        Job receipts stop at approval/parking boundaries. A Run wait spans those
        boundaries and uses the shared journal follower, without polling Run
        history or reloading checkpoints for each streamed token.
        """

        def boundary() -> tuple[RunRecord, int]:
            with self.store.session_read(session_id):
                run = self.store.load_run(session_id, run_id)
                return run, self.store.latest_event_sequence(session_id)

        job = self._jobs_by_session.get(session_id)
        if (
            job is not None
            and job.continue_existing
            and self._snapshots[job.handle.handle_id].run_id == run_id
        ):
            # A scheduled continuation reuses a terminal Run. Observe its
            # accepted attempt before interpreting the previous terminal state.
            await job.execution_started.wait()
        run, sequence = await run_blocking(boundary)
        if run.status not in ACTIVE_RUN_STATUSES or run.status in until:
            return run
        subscription = await self.subscribe(session_id, after_sequence=sequence)
        try:
            async for notification in subscription:
                diagnostic = notification.diagnostic
                if diagnostic is not None and diagnostic.code != "runtime_subscription_lagged":
                    raise OuroError(diagnostic.message)
                event = notification.event
                if diagnostic is None and (
                    event is None
                    or event.run_id != run_id
                    or event.event_type
                    not in {
                        "run_completed",
                        "run_failed",
                        "run_aborted",
                        "run_paused",
                        "run_status_changed",
                        "run_started",
                    }
                ):
                    continue
                run = await run_blocking(self.store.load_run, session_id, run_id)
                if run.status not in ACTIVE_RUN_STATUSES or run.status in until:
                    return run
            raise OuroError("Run observation closed before completion")
        finally:
            subscription.close()

    def release_handle(self, handle: RunHandle) -> None:
        """Release a producer's completed receipt; durable Session history remains available."""

        if handle.handle_id in self._jobs_by_handle:
            raise SessionBusyError("Cannot release a running handle")
        self._snapshots.pop(handle.handle_id, None)
        self._completion_events.pop(handle.handle_id, None)
        if (
            handle.session_id not in self._subscriptions
            and handle.session_id not in self._state_subscriptions
            and handle.session_id not in self._jobs_by_session
        ):
            self._activities.pop(handle.session_id, None)

    def detach_handle(self, handle: RunHandle) -> None:
        """Release Host interest now and discard its receipt when execution finishes."""
        job = self._jobs_by_handle.get(handle.handle_id)
        if job is None:
            self.release_handle(handle)
        else:
            job.detached = True

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
            durable = await run_blocking(self.status, handle_or_session)
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
            if job.host_input_receipt is not None and snapshot.run_id is None:
                for item in await run_blocking(
                    self.runtime.pending_user_inputs, job.handle.session_id
                ):
                    await self.runtime.cancel_pending_user_input(
                        job.handle.session_id, item.queue_id
                    )
            if snapshot.run_id is not None:
                await self.runtime.abort_run(
                    session_id=job.handle.session_id,
                    run_id=snapshot.run_id,
                    reason=reason,
                    _reservation=job.reservation,
                )
            aborted = RuntimeStatusSnapshot(
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
            )
            await self._finish_job(job, aborted)
            return aborted
        if snapshot.status in {
            "running",
            "cancelling",
            "waiting_user",
            "waiting_approval",
            "waiting_child",
        }:
            if snapshot.run_id is None:
                # A worker is between its process-local running transition and
                # the first durable Run event. Wait for that identity instead of
                # guessing the active Run from a snapshot that may still be empty.
                await job.run_identified.wait()
                identified = self._snapshots.get(job.handle.handle_id)
                if identified is None:
                    return await run_blocking(self.status, job.handle.session_id)
                snapshot = identified
                if job.done.is_set() or snapshot.run_id is None:
                    return snapshot
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
            if not job.done.is_set():
                self._snapshots[job.handle.handle_id] = updated
            return updated
        return snapshot

    async def shutdown(self, policy: ShutdownPolicy | None = None) -> None:
        async with self._lifecycle_lock:
            await self._shutdown(policy)

    async def _shutdown(self, policy: ShutdownPolicy | None = None) -> None:
        if self._closed:
            return
        policy = policy or ShutdownPolicy()
        # Accepted submissions must reach scheduling before shutdown chooses its jobs.
        await asyncio.gather(*self._input_admissions, return_exceptions=True)
        self._closed = True
        for task in tuple(self._host_dispatch_tasks):
            task.cancel()
        await asyncio.gather(*self._host_dispatch_tasks, return_exceptions=True)
        self._pending_host_inputs.clear()
        jobs = list(self._jobs_by_session.values())
        if policy.mode == "abort":
            # Stop queued jobs before awaiting active abort settlement. Otherwise
            # a worker can dequeue a not-yet-durable Run while shutdown yields.
            ordered = sorted(
                jobs,
                key=lambda job: self._snapshots[job.handle.handle_id].status != "queued",
            )
            for job in ordered:
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
                await self._finish_job(
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
                await self._finish_interrupted_job(job)
        for subscriptions in tuple(self._subscriptions.values()):
            for subscription in tuple(subscriptions):
                subscription.close()
        for state_subscriptions in tuple(self._state_subscriptions.values()):
            for state_subscription in tuple(state_subscriptions):
                state_subscription.close()
        await asyncio.gather(*self._event_stream_tasks)
        self._workers.clear()
        self._started = False
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
                if job.start_after is not None:
                    await job.start_after.wait()
                if job.cancelled:
                    continue
                queued_snapshot = self._snapshots[job.handle.handle_id]
                self._snapshots[job.handle.handle_id] = queued_snapshot.model_copy(
                    update={"status": "running", "updated_at": utc_now()}
                )
                current_activity = self._activities.get(job.handle.session_id)
                if current_activity is None and queued_snapshot.run_id is None:
                    current_activity = queued_runtime_activity(
                        job.handle.session_id, history_epoch=job.history_epoch or 0
                    )
                if current_activity is not None and current_activity.run_id is None:
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
                        job.provider_stream_started.set()
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
                        job.provider_stream_started.set()
                        run_id = self._snapshots[job.handle.handle_id].run_id
                        assert run_id is not None
                        job.result = await self.runtime.answer_user(
                            session_id=job.handle.session_id,
                            run_id=run_id,
                            answer=job.user_answer,
                        )
                    elif job.agent_wait_resolution is not None:
                        job.provider_stream_started.set()
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
                    elif job.continue_existing:
                        job.provider_stream_started.set()
                        job.result = await self.runtime.continue_run(
                            session_id=job.handle.session_id,
                            run_id=self._snapshots[job.handle.handle_id].run_id,
                            history_epoch=job.history_epoch,
                        )
                    elif job.prepared_run is not None:
                        job.provider_stream_started.set()
                        assert job.session_input is not None
                        job.result = await self.runtime.run_prepared(
                            job.prepared_run,
                            job.session_input,
                            reservation=job.reservation,
                        )
                    elif job.host_input_receipt is not None:
                        job.result = await self.runtime.consume_host_input(
                            job.host_input_receipt,
                            _on_provider_stream_start=job.provider_stream_started.set,
                        )
                    else:
                        job.result = await self.runtime.run(
                            job.prompt,
                            session_id=job.handle.session_id,
                            title=job.title,
                            _on_provider_stream_start=job.provider_stream_started.set,
                        )
                except asyncio.CancelledError:
                    await self._finish_interrupted_job(job)
                    raise
                except Exception as exc:  # noqa: BLE001 - task isolation boundary.
                    job.error = exc
                    diagnostic = Diagnostic(
                        code="supervised_run_failed",
                        severity="error",
                        component="runtime.supervisor",
                        message=f"runtime task failed ({type(exc).__name__})",
                        session_id=job.handle.session_id,
                        recoverable=False,
                    )
                    await self._finish_job(
                        job,
                        RuntimeStatusSnapshot(
                            handle=job.handle,
                            session_id=job.handle.session_id,
                            run_id=self._snapshots[job.handle.handle_id].run_id,
                            status="failed",
                            diagnostics=(diagnostic,),
                        ),
                    )
                else:
                    await self._finish_job(
                        job,
                        RuntimeStatusSnapshot(
                            handle=job.handle,
                            session_id=job.handle.session_id,
                            run_id=job.result.run.run_id,
                            status=job.result.run.status,
                        ),
                    )
            finally:
                job.provider_stream_started.set()
                self._queue.task_done()

    async def _on_event(self, event: EventRecord) -> None:
        job = self._jobs_by_session.get(event.session_id)
        if (
            job is not None
            and not job.done.is_set()
            and (job.history_epoch is None or event.history_epoch >= job.history_epoch)
        ):
            current = self._snapshots[job.handle.handle_id]
            status = _status_from_event(event, current.status)
            self._snapshots[job.handle.handle_id] = current.model_copy(
                update={
                    "run_id": event.run_id,
                    "status": status,
                    "updated_at": utc_now(),
                }
            )
            job.run_identified.set()
            if event.event_type == "run_started" or (
                event.event_type == "run_status_changed" and event.payload.get("resumed") is True
            ):
                job.execution_started.set()
        subscriptions = tuple(self._subscriptions.get(event.session_id, ()))
        stream = self._event_streams.get(event.session_id)
        if stream is not None:
            stream.offer(event)
            return
        previous_activity = self._activities.get(event.session_id)
        if previous_activity is not None and event.history_epoch < previous_activity.history_epoch:
            return
        notification = RuntimeNotification(kind="event", event=project_runtime_event(event))
        for subscription in subscriptions:
            subscription._offer(notification)
        if previous_activity is None:
            current_activity = await run_blocking(self._load_activity, event.session_id)
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

    def _publish_observed_event(self, event: EventRecord) -> None:
        previous = self._activities.get(event.session_id)
        if previous is not None and event.history_epoch < previous.history_epoch:
            return
        notification = RuntimeNotification(kind="event", event=project_runtime_event(event))
        for subscription in tuple(self._subscriptions.get(event.session_id, ())):
            subscription._offer(notification)
        for state_subscription in tuple(self._state_subscriptions.get(event.session_id, ())):
            if event.event_type in state_subscription.event_types:
                state_subscription.offer(notification)
        if previous is not None:
            self._publish_activity(reduce_runtime_activity(previous, event))

    def _report_observation_error(self, diagnostic: Diagnostic) -> None:
        assert diagnostic.session_id is not None
        stream = self._event_streams.pop(diagnostic.session_id, None)
        if stream is not None:
            stream.close()
        for subscription in tuple(self._subscriptions.get(diagnostic.session_id, ())):
            subscription.fail(diagnostic)
        for state_subscription in tuple(self._state_subscriptions.get(diagnostic.session_id, ())):
            state_subscription.fail(diagnostic)

    def _load_activity(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        status_override: SupervisorRunStatus | None = None,
    ) -> RuntimeActivitySnapshot:
        # The Run and cursor must describe one read boundary. Reading a newer
        # cursor after an older Run would permanently hide its terminal event.
        with self.store.session_read(session_id):
            session = self.store.load_session_record(session_id)
            selected_run_id = run_id or session.active_run_id or session.last_run_id
            run = (
                self.store.load_run(session_id, selected_run_id)
                if selected_run_id is not None
                else None
            )
            sequence = self.store.latest_event_sequence(session_id)
            context_status = self.runtime.sessions.context_status(session_id)
            metrics = self.runtime.sessions.effective_context_metrics(session_id)
        return project_runtime_activity_state(
            session,
            run,
            sequence=sequence,
            status_override=status_override if run is None else None,
            context_status=context_status,
            effective_context_metrics=metrics,
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

    async def _finish_job(self, job: _RunJob, snapshot: RuntimeStatusSnapshot) -> None:
        if job.reservation is not None:
            await run_blocking(job.reservation.release)
        job.provider_stream_started.set()
        job.run_identified.set()
        job.execution_started.set()
        self._snapshots[job.handle.handle_id] = snapshot
        current_activity = self._activities.get(job.handle.session_id)
        # A job is one process's execution attempt. Once a Run is identified,
        # only its ordered events may advance the shared Session projection:
        # another Host may already have resumed or finished this same Run.
        if current_activity is None and snapshot.run_id is None:
            current_activity = RuntimeActivitySnapshot(
                session_id=job.handle.session_id,
                run_id=snapshot.run_id,
                status=snapshot.status,
                activity=runtime_activity_for_status(snapshot.status),
            )
        elif current_activity is not None and current_activity.run_id is None:
            current_activity = current_activity.model_copy(
                update={
                    "run_id": snapshot.run_id or current_activity.run_id,
                    "status": snapshot.status,
                    "activity": runtime_activity_for_status(snapshot.status),
                    "updated_at": utc_now(),
                }
            )
        if current_activity is not None:
            self._publish_activity(current_activity)
        if self._jobs_by_session.get(job.handle.session_id) is job:
            self._jobs_by_session.pop(job.handle.session_id, None)
        if self._jobs_by_handle.get(job.handle.handle_id) is job:
            self._jobs_by_handle.pop(job.handle.handle_id, None)
        job.done.set()
        if job.host_input_receipt is not None or job.detached:
            # Host producers receive the durable input receipt, never this
            # private execution handle; do not retain it for process lifetime.
            self.release_handle(job.handle)
        if not self._closed and job.handle.session_id in self._pending_host_inputs:
            task = asyncio.create_task(
                self._dispatch_after_job(job.handle.session_id),
                name=f"ouro-host-input-{job.handle.session_id}",
            )
            self._host_dispatch_tasks.add(task)
            task.add_done_callback(self._host_dispatch_finished)

    def _publish_activity(self, activity: RuntimeActivitySnapshot) -> None:
        previous = self._activities.get(activity.session_id)
        self._activities[activity.session_id] = activity
        if previous is not None and not runtime_activity_changed(previous, activity):
            return
        notification = RuntimeNotification(kind="activity", activity=activity)
        for state_subscription in tuple(self._state_subscriptions.get(activity.session_id, ())):
            state_subscription.offer(notification)
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

    def _load_active_run(self, session_id: str) -> RunRecord | None:
        with self.store.session_read(session_id):
            session = self.store.load_session_record(session_id)
            return maybe_active_run(
                self.store.load_runs(session_id), history_epoch=session.history_epoch
            )

    async def _finish_interrupted_job(self, job: _RunJob) -> None:
        try:
            active = await run_blocking(self._load_active_run, job.handle.session_id)
        except Exception:  # noqa: BLE001 - shutdown must still release process state.
            active = None
        await self._finish_job(
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
            "cancelling",
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
        "run_abort_requested": "cancelling",
        "approval_requested": "waiting_approval",
        "ask_user_requested": "waiting_user",
        "run_completed": "completed",
        "run_aborted": "aborted",
        "run_failed": "failed",
        "run_paused": "paused",
    }
    return mapping.get(event.event_type, current)
