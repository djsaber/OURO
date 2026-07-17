from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from inspect import isawaitable, iscoroutinefunction
from typing import Any

from ouro.application.event_models import validate_runtime_event_payload
from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookFailure, HookRegistry
from ouro.core.redaction import redact_text
from ouro.sessions import SessionRepository

RuntimeEventSink = Callable[[EventRecord], None | Awaitable[None]]
HookRegistryResolver = Callable[[str | None], tuple[HookRegistry, Callable[[], None]]]


@dataclass
class _SinkObserver:
    callback: RuntimeEventSink
    queue: asyncio.Queue[EventRecord | None]
    task: asyncio.Task[None]
    enabled: bool = True


@dataclass(frozen=True)
class _HookObservation:
    event: EventRecord
    hooks: HookRegistry
    release: Callable[[], None]


class RuntimeEventRecorder:
    """Persist canonical events, then fan them out through bounded observers.

    Persistence is the only required part of ``emit``. Host sinks and ``on_event``
    hooks are observational: each runs outside the durable critical path with a
    bounded queue and timeout, so one UI or plugin cannot stall another Session.
    """

    def __init__(
        self,
        *,
        store: SessionRepository,
        hooks: HookRegistry,
        event_sink: RuntimeEventSink | None = None,
        observer_queue_size: int = 256,
        sink_timeout_seconds: float = 1.0,
    ) -> None:
        if observer_queue_size <= 0:
            raise ValueError("observer_queue_size must be positive")
        if sink_timeout_seconds <= 0:
            raise ValueError("sink_timeout_seconds must be positive")
        self.store = store
        self.hooks = hooks
        self._observer_queue_size = observer_queue_size
        self._sink_timeout_seconds = sink_timeout_seconds
        self._sink_callbacks: list[RuntimeEventSink] = []
        self._sink_observers: list[_SinkObserver] = []
        self._hook_registry_resolver: HookRegistryResolver | None = None
        self._hook_queue: asyncio.Queue[_HookObservation | None] | None = None
        self._hook_task: asyncio.Task[None] | None = None
        self._diagnostic_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        if event_sink is not None:
            self.add_event_sink(event_sink)

    def add_event_sink(self, event_sink: RuntimeEventSink) -> None:
        if self._closed:
            raise RuntimeError("event recorder is closed")
        self._sink_callbacks.append(event_sink)

    def set_hook_registry_resolver(self, resolver: HookRegistryResolver) -> None:
        """Resolve and retain the immutable Hook view for each observed event."""

        if self._closed:
            raise RuntimeError("event recorder is closed")
        self._hook_registry_resolver = resolver

    async def emit(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        session_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        validate_runtime_event_payload(event_type, payload)
        event = EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id=(payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None),
            event_type=event_type,
            sequence=sequence + 1,
            payload=payload,
        )
        event = await self._append_ordered_event(event)
        emitted.append(event)
        self._publish(event, notify_hooks=True)
        return event.sequence

    async def record_hook_failures(
        self,
        emitted: list[EventRecord],
        sequence: int,
        failures: Sequence[HookFailure],
    ) -> int:
        for failure in failures:
            event = await self._append_ordered_event(
                EventRecord(
                    session_id=failure.session_id or "unknown",
                    run_id=failure.run_id or "unknown",
                    event_type="plugin_hook_failed",
                    sequence=sequence + 1,
                    payload=failure.model_dump(mode="json"),
                )
            )
            sequence = event.sequence
            emitted.append(event)
            self._publish(event, notify_hooks=False)
        return sequence

    async def drain(self) -> None:
        """Wait until all observations and generated diagnostics have settled."""

        self._ensure_observers_started()
        await self._join_observer_queues()
        while self._diagnostic_tasks:
            tasks = tuple(self._diagnostic_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._join_observer_queues()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.drain()
        tasks: list[asyncio.Task[None]] = []
        if self._hook_queue is not None and self._hook_task is not None:
            self._hook_queue.put_nowait(None)
            tasks.append(self._hook_task)
        for observer in self._sink_observers:
            if not observer.task.done():
                observer.queue.put_nowait(None)
                tasks.append(observer.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _append_ordered_event(self, event: EventRecord) -> EventRecord:
        return await asyncio.to_thread(self.store.append_ordered_event, event)

    def _publish(self, event: EventRecord, *, notify_hooks: bool) -> None:
        if self._closed:
            return
        self._ensure_observers_started()
        for observer in self._sink_observers:
            if not observer.enabled:
                continue
            try:
                observer.queue.put_nowait(event)
            except asyncio.QueueFull:
                self._disable_sink(
                    observer,
                    event=event,
                    error_type="ObserverQueueFull",
                    error_message=(
                        f"event sink exceeded its {self._observer_queue_size}-event backlog"
                    ),
                )
        if notify_hooks and self._hook_queue is not None:
            observation = self._hook_observation(event)
            try:
                self._hook_queue.put_nowait(observation)
            except asyncio.QueueFull:
                observation.release()
                self._schedule_diagnostic(
                    event,
                    event_type="event_observer_overflow",
                    payload={
                        "observer": "plugin_hooks",
                        "source_event_type": event.event_type,
                        "queue_size": self._observer_queue_size,
                    },
                )

    def _ensure_observers_started(self) -> None:
        if self._hook_queue is None:
            self._hook_queue = asyncio.Queue(maxsize=self._observer_queue_size)
            self._hook_task = asyncio.create_task(
                self._run_hook_observer(self._hook_queue),
                name="ouro-event-hooks",
            )
        while len(self._sink_observers) < len(self._sink_callbacks):
            callback = self._sink_callbacks[len(self._sink_observers)]
            queue: asyncio.Queue[EventRecord | None] = asyncio.Queue(
                maxsize=self._observer_queue_size
            )
            task = asyncio.create_task(
                self._run_sink_observer(callback, queue),
                name=f"ouro-event-sink-{len(self._sink_observers)}",
            )
            self._sink_observers.append(_SinkObserver(callback=callback, queue=queue, task=task))

    async def _run_sink_observer(
        self,
        callback: RuntimeEventSink,
        queue: asyncio.Queue[EventRecord | None],
    ) -> None:
        observer = next(item for item in self._sink_observers if item.callback is callback)
        while True:
            event = await queue.get()
            try:
                if event is None:
                    return
                if not observer.enabled:
                    continue
                try:
                    await asyncio.wait_for(
                        _invoke_sink(callback, event),
                        timeout=self._sink_timeout_seconds,
                    )
                except TimeoutError:
                    self._disable_sink(
                        observer,
                        event=event,
                        error_type="TimeoutError",
                        error_message=(
                            f"event sink timed out after {self._sink_timeout_seconds:g} seconds"
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - host callback isolation boundary.
                    self._disable_sink(
                        observer,
                        event=event,
                        error_type=type(exc).__name__,
                        error_message=redact_text(str(exc)),
                    )
            finally:
                queue.task_done()

    async def _run_hook_observer(
        self,
        queue: asyncio.Queue[_HookObservation | None],
    ) -> None:
        while True:
            observation = await queue.get()
            try:
                if observation is None:
                    return
                event = observation.event
                result = await observation.hooks.dispatch(
                    HookContext(
                        hook_name="on_event",
                        session_id=event.session_id,
                        run_id=event.run_id,
                        data={"event": event.model_dump(mode="json")},
                    )
                )
                for failure in result.failures:
                    diagnostic = await self._append_ordered_event(
                        EventRecord(
                            session_id=failure.session_id or event.session_id,
                            run_id=failure.run_id or event.run_id,
                            event_type="plugin_hook_failed",
                            payload=failure.model_dump(mode="json"),
                        )
                    )
                    self._publish(diagnostic, notify_hooks=False)
            finally:
                if observation is not None:
                    observation.release()
                queue.task_done()

    def _hook_observation(self, event: EventRecord) -> _HookObservation:
        if self._hook_registry_resolver is None:
            return _HookObservation(event=event, hooks=self.hooks, release=lambda: None)
        hooks, release = self._hook_registry_resolver(event.run_id)
        return _HookObservation(event=event, hooks=hooks, release=release)

    def _disable_sink(
        self,
        observer: _SinkObserver,
        *,
        event: EventRecord,
        error_type: str,
        error_message: str,
    ) -> None:
        if not observer.enabled:
            return
        observer.enabled = False
        while True:
            try:
                queued = observer.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                observer.queue.task_done()
                if queued is None:
                    break
        self._schedule_diagnostic(
            event,
            event_type="event_sink_failed",
            payload={
                "source_event_type": event.event_type,
                "error_type": error_type,
                "error_message": error_message,
            },
        )

    def _schedule_diagnostic(
        self,
        source: EventRecord,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        task = asyncio.create_task(
            self._persist_observer_diagnostic(source, event_type=event_type, payload=payload),
            name=f"ouro-{event_type}",
        )
        self._diagnostic_tasks.add(task)
        task.add_done_callback(self._diagnostic_tasks.discard)

    async def _persist_observer_diagnostic(
        self,
        source: EventRecord,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        diagnostic = await self._append_ordered_event(
            EventRecord(
                session_id=source.session_id,
                run_id=source.run_id,
                event_type=event_type,
                payload=payload,
            )
        )
        self._publish(diagnostic, notify_hooks=False)

    async def _join_observer_queues(self) -> None:
        joins = [observer.queue.join() for observer in self._sink_observers]
        if self._hook_queue is not None:
            joins.append(self._hook_queue.join())
        if joins:
            await asyncio.gather(*joins)


async def _invoke_sink(callback: RuntimeEventSink, event: EventRecord) -> None:
    if iscoroutinefunction(callback):
        await callback(event)
        return
    result = await asyncio.to_thread(callback, event)
    if isawaitable(result):
        await result
