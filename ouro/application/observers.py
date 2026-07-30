from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ouro.core.callbacks import invoke_bounded_callback
from ouro.core.observers import (
    Observation,
    ObserverFailure,
    ObserverRegistration,
    ObserverRegistry,
)
from ouro.core.redaction import redact_text

ObserverFailureSink = Callable[[ObserverFailure], None | Awaitable[None]]
ReleaseCallback = Callable[[], None]
type _ScopeKey = tuple[str, str]


@dataclass
class _SharedRelease:
    callback: ReleaseCallback
    remaining: int
    released: bool = False

    def release_one(self) -> None:
        if self.released:
            return
        self.remaining -= 1
        if self.remaining > 0:
            return
        self.released = True
        self.callback()


@dataclass(frozen=True)
class _QueuedObservation:
    observation: Observation
    release: ReleaseCallback


@dataclass
class _Mailbox:
    registration: ObserverRegistration
    queue: asyncio.Queue[_QueuedObservation]
    task: asyncio.Task[None] | None = None
    disabled_scopes: set[_ScopeKey] = field(default_factory=set)


class ObserverDispatcher:
    """Non-blocking, per-registration delivery owned by the application layer."""

    def __init__(
        self,
        *,
        mailbox_size: int = 64,
        failure_queue_size: int = 256,
        shutdown_timeout_seconds: float = 5.0,
    ) -> None:
        if mailbox_size <= 0 or failure_queue_size <= 0:
            raise ValueError("observer queue sizes must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("observer shutdown timeout must be positive")
        self._mailbox_size = mailbox_size
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._mailboxes: dict[
            tuple[str, str, str, str],
            _Mailbox,
        ] = {}
        self._disabled_scopes: dict[
            tuple[str, str, str, str],
            set[_ScopeKey],
        ] = {}
        self._failure_sink: ObserverFailureSink | None = None
        self._failure_queue: asyncio.Queue[ObserverFailure] = asyncio.Queue(
            maxsize=failure_queue_size
        )
        self._failure_task: asyncio.Task[None] | None = None
        self._run_pending: dict[str, int] = {}
        self._finished_runs: set[str] = set()
        self._closed = False

    def set_failure_sink(self, sink: ObserverFailureSink) -> None:
        if self._closed:
            raise RuntimeError("observer dispatcher is closed")
        self._failure_sink = sink

    def enqueue(
        self,
        registry: ObserverRegistry,
        observation: Observation,
        *,
        release: ReleaseCallback = lambda: None,
    ) -> None:
        if self._closed:
            release()
            return
        registrations = registry.matching(observation)
        if not registrations:
            release()
            return
        shared = _SharedRelease(release, len(registrations))
        scope = _observation_scope(observation)
        for registration in registrations:
            disabled = self._disabled_scopes.get(registration.delivery_key)
            if disabled is not None and scope in disabled:
                shared.release_one()
                continue
            mailbox = self._mailbox(registration)
            queued = _QueuedObservation(observation=observation, release=shared.release_one)
            try:
                mailbox.queue.put_nowait(queued)
            except asyncio.QueueFull:
                mailbox.disabled_scopes.add(scope)
                shared.release_one()
                self._queue_failure(
                    _failure(
                        registration,
                        observation,
                        error_type="ObserverQueueFull",
                        message=(f"observer exceeded its {self._mailbox_size}-item backlog"),
                    )
                )
                continue
            if observation.run_id is not None:
                self._run_pending[observation.run_id] = (
                    self._run_pending.get(observation.run_id, 0) + 1
                )

    def finish_run(self, run_id: str) -> None:
        self._finished_runs.add(run_id)
        self._finish_run_if_drained(run_id)

    async def drain(self) -> None:
        while True:
            tasks = [
                mailbox.task for mailbox in self._mailboxes.values() if mailbox.task is not None
            ]
            if self._failure_task is not None:
                tasks.append(self._failure_task)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        mailboxes = tuple(self._mailboxes.values())
        drain_task = asyncio.create_task(
            self.drain(),
            name="ouro-observer-drain",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(drain_task),
                timeout=self._shutdown_timeout_seconds,
            )
        except TimeoutError:
            await self._cancel_pending(mailboxes)
            await asyncio.gather(drain_task, return_exceptions=True)
        except BaseException:
            await self._cancel_pending(mailboxes)
            await asyncio.gather(drain_task, return_exceptions=True)
            raise
        self._mailboxes.clear()
        self._disabled_scopes.clear()
        self._run_pending.clear()
        self._finished_runs.clear()

    def _mailbox(self, registration: ObserverRegistration) -> _Mailbox:
        key = registration.delivery_key
        mailbox = self._mailboxes.get(key)
        if mailbox is not None:
            return mailbox
        mailbox = _Mailbox(
            registration=registration,
            queue=asyncio.Queue(maxsize=self._mailbox_size),
            disabled_scopes=self._disabled_scopes.setdefault(key, set()),
        )
        self._mailboxes[key] = mailbox
        mailbox.task = asyncio.create_task(
            self._consume_mailbox(key, mailbox),
            name=f"ouro-observer-{registration.plugin_id}-{registration.observer_id}",
        )
        return mailbox

    async def _consume_mailbox(
        self,
        key: tuple[str, str, str, str],
        mailbox: _Mailbox,
    ) -> None:
        try:
            while True:
                queued = await mailbox.queue.get()
                observation = queued.observation
                scope = _observation_scope(observation)
                try:
                    if scope not in mailbox.disabled_scopes:
                        try:
                            result = await invoke_bounded_callback(
                                mailbox.registration.handler,
                                observation,
                                timeout_seconds=mailbox.registration.timeout_seconds,
                            )
                            if result is not None:
                                raise TypeError("observer handler must return None")
                        except TimeoutError:
                            mailbox.disabled_scopes.add(scope)
                            self._queue_failure(
                                _failure(
                                    mailbox.registration,
                                    observation,
                                    error_type="TimeoutError",
                                    message=(
                                        "observer timed out after "
                                        f"{mailbox.registration.timeout_seconds:g} seconds"
                                    ),
                                )
                            )
                        except Exception as exc:  # noqa: BLE001 - isolation boundary.
                            mailbox.disabled_scopes.add(scope)
                            self._queue_failure(
                                _failure(
                                    mailbox.registration,
                                    observation,
                                    error_type=type(exc).__name__,
                                    message=redact_text(str(exc)),
                                )
                            )
                finally:
                    queued.release()
                    mailbox.queue.task_done()
                    self._item_finished(observation.run_id)
                if mailbox.queue.empty():
                    if self._mailboxes.get(key) is mailbox:
                        self._mailboxes.pop(key, None)
                    return
        finally:
            if self._mailboxes.get(key) is mailbox:
                self._mailboxes.pop(key, None)
            if not mailbox.disabled_scopes:
                self._disabled_scopes.pop(key, None)

    def _queue_failure(self, failure: ObserverFailure) -> None:
        try:
            self._failure_queue.put_nowait(failure)
        except asyncio.QueueFull:
            return
        if self._failure_task is None or self._failure_task.done():
            self._failure_task = asyncio.create_task(
                self._consume_failures(),
                name="ouro-observer-diagnostics",
            )

    async def _consume_failures(self) -> None:
        try:
            while True:
                failure = await self._failure_queue.get()
                try:
                    if self._failure_sink is not None:
                        result = self._failure_sink(failure)
                        if isinstance(result, Awaitable):
                            await result
                except Exception:  # noqa: BLE001 - diagnostics cannot break observers.
                    pass
                finally:
                    self._failure_queue.task_done()
                if self._failure_queue.empty():
                    return
        finally:
            self._failure_task = None

    def _item_finished(self, run_id: str | None) -> None:
        if run_id is None:
            return
        remaining = self._run_pending.get(run_id, 0) - 1
        if remaining > 0:
            self._run_pending[run_id] = remaining
        else:
            self._run_pending.pop(run_id, None)
        self._finish_run_if_drained(run_id)

    def _finish_run_if_drained(self, run_id: str) -> None:
        if run_id not in self._finished_runs or self._run_pending.get(run_id, 0) > 0:
            return
        scope = ("run", run_id)
        for key, disabled in list(self._disabled_scopes.items()):
            disabled.discard(scope)
            if not disabled and key not in self._mailboxes:
                self._disabled_scopes.pop(key, None)
        self._finished_runs.discard(run_id)

    async def _cancel_pending(self, mailboxes: tuple[_Mailbox, ...]) -> None:
        tasks = [
            mailbox.task
            for mailbox in mailboxes
            if mailbox.task is not None and not mailbox.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for mailbox in mailboxes:
            while True:
                try:
                    queued = mailbox.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                queued.release()
                mailbox.queue.task_done()
                self._item_finished(queued.observation.run_id)
        if self._failure_task is not None and not self._failure_task.done():
            self._failure_task.cancel()
            await asyncio.gather(self._failure_task, return_exceptions=True)


def _observation_scope(observation: Observation) -> _ScopeKey:
    if observation.run_id is not None:
        return ("run", observation.run_id)
    return ("session", observation.session_id)


def _failure(
    registration: ObserverRegistration,
    observation: Observation,
    *,
    error_type: str,
    message: str,
) -> ObserverFailure:
    return ObserverFailure(
        plugin_id=registration.plugin_id,
        observer_id=registration.observer_id,
        observation=observation.event,
        session_id=observation.session_id,
        run_id=observation.run_id,
        error_type=error_type,
        message=message,
    )


__all__ = ["ObserverDispatcher", "ObserverFailureSink"]
