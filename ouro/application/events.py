from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from inspect import isawaitable, iscoroutinefunction
from typing import Any, Protocol, cast

from ouro.application.contracts.events import validate_runtime_event_payload
from ouro.application.harness import capture_harness
from ouro.application.observations.drafts import resolve_fact_event
from ouro.application.observers import ObserverDispatcher
from ouro.application.usage import UsageService
from ouro.core.callbacks import run_blocking
from ouro.core.cancellation import settle_owned_tasks
from ouro.core.context_providers import ContextProviderFailure
from ouro.core.events import EventRecord
from ouro.core.observers import (
    FrozenJson,
    Observation,
    ObserverFailure,
    ObserverRegistry,
    RuntimeEventObservation,
    RuntimeEventView,
    freeze_json,
)
from ouro.core.redaction import redact_data, redact_text
from ouro.core.response_drafts import ResponseStreamPosition
from ouro.core.turn_policies import TurnPolicyFailure
from ouro.sessions import SessionRepository

RuntimeEventSink = Callable[[EventRecord], None | Awaitable[None]]
RuntimeEventFilter = Callable[[EventRecord], bool]
ObserverRegistryResolver = Callable[
    [Observation],
    tuple[ObserverRegistry, Callable[[], None]],
]
RuntimeEventObserverMatcher = Callable[[str | None, str], bool]


class ApplicationEventPublisher(Protocol):
    async def publish(
        self,
        *,
        session_id: str,
        run_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> EventRecord: ...


@dataclass
class _SinkObserver:
    callback: RuntimeEventSink
    queue: asyncio.Queue[EventRecord | None]
    event_filter: RuntimeEventFilter | None = None
    task: asyncio.Task[None] | None = None
    enabled: bool = True


class RuntimeEventSinkRegistration:
    """One detachable Host event sink owned by its registering consumer."""

    def __init__(self, recorder: RuntimeEventRecorder, observer: _SinkObserver) -> None:
        self._recorder = recorder
        self._observer = observer
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._recorder._remove_event_sink(self._observer)


class RuntimeEventRecorder:
    """Persist canonical events, then fan them out through bounded observers.

    Persistence is the only required part of ``emit``. Host sinks and plugin
    Observers run outside the durable critical path with independent bounded
    delivery, so one UI or plugin cannot stall another Session.
    """

    def __init__(
        self,
        *,
        store: SessionRepository,
        observers: ObserverRegistry,
        event_sink: RuntimeEventSink | None = None,
        observer_queue_size: int = 256,
        sink_timeout_seconds: float = 1.0,
        observer_dispatcher: ObserverDispatcher | None = None,
        usage_service: UsageService | None = None,
    ) -> None:
        if observer_queue_size <= 0:
            raise ValueError("observer_queue_size must be positive")
        if sink_timeout_seconds <= 0:
            raise ValueError("sink_timeout_seconds must be positive")
        self.store = store
        # Run history ownership is immutable; avoid reading a Run for every delta.
        self._run_history_epoch = lru_cache(maxsize=128)(self._load_run_history_epoch)
        self.usage_service = usage_service
        self.observers = observers
        self._observer_queue_size = observer_queue_size
        self._sink_timeout_seconds = sink_timeout_seconds
        self._sink_observers: list[_SinkObserver] = []
        self._observer_registry_resolver: ObserverRegistryResolver | None = None
        self._runtime_event_observer_matcher: RuntimeEventObserverMatcher | None = None
        self._observer_dispatcher = observer_dispatcher or ObserverDispatcher()
        self._observer_dispatcher.set_failure_sink(self.record_observer_failure)
        self._diagnostic_tasks: set[asyncio.Task[None]] = set()
        self._session_event_locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        if event_sink is not None:
            self.add_event_sink(event_sink)

    def add_event_sink(
        self,
        event_sink: RuntimeEventSink,
        *,
        event_filter: RuntimeEventFilter | None = None,
    ) -> RuntimeEventSinkRegistration:
        """Register a Host consumer with an optional pre-enqueue selection.

        Filters run synchronously on committed event metadata and must not block
        or perform I/O. A rejected event remains durable and available to other
        consumers; a failing filter disables only its own sink.
        """

        if self._closed:
            raise RuntimeError("event recorder is closed")
        observer = _SinkObserver(
            callback=event_sink,
            queue=asyncio.Queue(maxsize=self._observer_queue_size),
            event_filter=event_filter,
        )
        self._sink_observers.append(observer)
        return RuntimeEventSinkRegistration(self, observer)

    def set_observer_registry_resolver(self, resolver: ObserverRegistryResolver) -> None:
        """Resolve and retain the immutable Observer view for one fact."""

        if self._closed:
            raise RuntimeError("event recorder is closed")
        self._observer_registry_resolver = resolver

    def set_runtime_event_observer_matcher(
        self,
        matcher: RuntimeEventObserverMatcher,
    ) -> None:
        """Bind a lightweight predicate for avoiding unused event projections."""

        if self._closed:
            raise RuntimeError("event recorder is closed")
        self._runtime_event_observer_matcher = matcher

    async def observe(self, observation: Observation) -> None:
        """Capture committed state, then enqueue without awaiting plugin code."""

        if self._closed:
            return
        if self._observer_registry_resolver is None:
            registry = self.observers
            release = _noop_release
        else:
            registry, release = self._observer_registry_resolver(observation)
        try:
            if not registry.matching(observation):
                release()
                return
            observation = replace(
                observation,
                harness=await run_blocking(
                    capture_harness,
                    self.store,
                    observation.session_id,
                    usage_service=self.usage_service,
                ),
            )
        except BaseException:
            release()
            raise
        self._observer_dispatcher.enqueue(registry, observation, release=release)

    def finish_run_observers(self, run_id: str) -> None:
        self._observer_dispatcher.finish_run(run_id)

    async def emit(
        self,
        sequence: int,
        *,
        session_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        stream: ResponseStreamPosition | None = None,
        history_epoch: int | None = None,
    ) -> int:
        validate_runtime_event_payload(event_type, payload)
        event = EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id=(payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None),
            event_type=event_type,
            sequence=sequence + 1,
            payload=payload,
            stream=stream,
        )
        if stream is not None:
            if history_epoch is None:
                raise ValueError("stream publication requires its Run history epoch")
            event = event.model_copy(
                update={
                    "sequence": sequence,
                    "history_epoch": history_epoch,
                }
            )
            await self._publish(event, notify_observers=True)
            return sequence
        event = await self._record_event(event, notify_observers=True)
        return event.sequence

    async def publish(
        self,
        *,
        session_id: str,
        run_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> EventRecord:
        """Persist and publish an application event outside a Run loop transaction."""

        validate_runtime_event_payload(event_type, payload)
        return await self._record_event(
            EventRecord(
                session_id=session_id,
                run_id=run_id,
                event_type=event_type,
                payload=payload,
            ),
            notify_observers=True,
        )

    async def publish_committed(self, events: Sequence[EventRecord]) -> None:
        """Notify from one native commit; lost delivery is repaired by fact catch-up."""
        for event in events:
            if event.event_type in {
                "model_response_committed",
                "tool_call_requested",
                "tool_call_completed",
            }:
                event = await run_blocking(self._fact_event, event)
            await self._publish(event, notify_observers=True)

    def _fact_event(self, event: EventRecord) -> EventRecord:
        with self.store.session_read(event.session_id):
            epoch = self.store.load_session_record(event.session_id).history_epoch
            return resolve_fact_event(self.store, event, history_epoch=epoch)

    async def record_observer_failure(self, failure: ObserverFailure) -> None:
        await self._record_event(
            EventRecord(
                session_id=failure.session_id or "unknown",
                run_id=failure.run_id,
                event_type="plugin_observer_failed",
                payload=failure.model_dump(mode="json"),
            ),
            notify_observers=False,
        )

    async def record_context_provider_failures(
        self,
        sequence: int,
        failures: Sequence[ContextProviderFailure],
    ) -> int:
        for failure in failures:
            event = await self._record_event(
                EventRecord(
                    session_id=failure.session_id,
                    run_id=failure.run_id,
                    event_type="context_provider_failed",
                    sequence=sequence + 1,
                    payload=failure.model_dump(mode="json"),
                ),
                notify_observers=False,
            )
            sequence = event.sequence
        return sequence

    async def record_turn_policy_failures(
        self,
        sequence: int,
        failures: Sequence[TurnPolicyFailure],
    ) -> int:
        for failure in failures:
            event = await self._record_event(
                EventRecord(
                    session_id=failure.session_id,
                    run_id=failure.run_id,
                    turn_id=failure.turn_id,
                    event_type="turn_policy_failed",
                    sequence=sequence + 1,
                    payload=failure.model_dump(mode="json"),
                ),
                notify_observers=False,
            )
            sequence = event.sequence
        return sequence

    async def drain(self) -> None:
        """Wait until all observations and generated diagnostics have settled."""

        self._ensure_sink_observers_started()
        await self._observer_dispatcher.drain()
        await self._join_sink_queues()
        while self._diagnostic_tasks:
            tasks = tuple(self._diagnostic_tasks)
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._join_sink_queues()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._observer_dispatcher.close()
        await self._join_sink_queues()
        while self._diagnostic_tasks:
            diagnostic_tasks = tuple(self._diagnostic_tasks)
            await asyncio.gather(*diagnostic_tasks, return_exceptions=True)
            await self._join_sink_queues()
        sink_tasks: list[asyncio.Task[None]] = []
        for observer in self._sink_observers:
            if observer.task is not None and not observer.task.done():
                observer.queue.put_nowait(None)
                sink_tasks.append(observer.task)
        if sink_tasks:
            await asyncio.gather(*sink_tasks, return_exceptions=True)

    async def _append_ordered_event(self, event: EventRecord) -> EventRecord:
        return await asyncio.to_thread(self._append_event_in_history, event)

    def _load_run_history_epoch(self, session_id: str, run_id: str) -> int:
        return self.store.load_run(session_id, run_id).history_epoch

    def _append_event_in_history(self, event: EventRecord) -> EventRecord:
        epoch = (
            self._run_history_epoch(event.session_id, event.run_id)
            if event.run_id is not None
            else self.store.load_session_record(event.session_id).history_epoch
        )
        return self.store.append_ordered_event(event.model_copy(update={"history_epoch": epoch}))

    async def _record_event(
        self,
        event: EventRecord,
        *,
        notify_observers: bool,
    ) -> EventRecord:
        """Keep persistence order and live fan-out order identical per Session."""

        lock = self._session_event_locks.setdefault(event.session_id, asyncio.Lock())
        async with lock:
            # Keep the session lock through both persistence and publication,
            # including when the owner is cancelled during a thread write.
            task = asyncio.create_task(
                self._persist_and_publish(event, notify_observers=notify_observers)
            )
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await settle_owned_tasks(task)
                if not task.cancelled():
                    task.exception()
                raise

    async def _persist_and_publish(
        self, event: EventRecord, *, notify_observers: bool
    ) -> EventRecord:
        recorded = await self._append_ordered_event(event)
        await self._publish(recorded, notify_observers=notify_observers)
        return recorded

    async def _publish(self, event: EventRecord, *, notify_observers: bool) -> None:
        if self._closed:
            return
        self._ensure_sink_observers_started()
        for observer in self._sink_observers:
            if not observer.enabled:
                continue
            try:
                if observer.event_filter is not None and not observer.event_filter(event):
                    continue
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
            except Exception as exc:  # noqa: BLE001 - isolate Host selection failures.
                self._disable_sink(
                    observer,
                    event=event,
                    error_type=type(exc).__name__,
                    error_message=redact_text(str(exc)),
                )
        if notify_observers and self._has_runtime_event_observer(event):
            await self.observe(_runtime_event_observation(event))

    def _has_runtime_event_observer(self, event: EventRecord) -> bool:
        matcher = self._runtime_event_observer_matcher
        if matcher is not None:
            try:
                return matcher(event.run_id, event.event_type)
            except Exception:
                return True
        if self._observer_registry_resolver is not None:
            return True
        return self.observers.has_registration(
            "runtime_event",
            runtime_event_type=event.event_type,
        )

    def _ensure_sink_observers_started(self) -> None:
        for index, observer in enumerate(self._sink_observers):
            if observer.task is not None or not observer.enabled:
                continue
            # Eager task factories may enter the consumer before create_task()
            # returns, so its complete lifecycle state must already be published.
            observer.task = asyncio.create_task(
                self._run_sink_observer(observer),
                name=f"ouro-event-sink-{index}",
            )

    async def _remove_event_sink(self, observer: _SinkObserver) -> None:
        if observer not in self._sink_observers:
            return
        self._sink_observers.remove(observer)
        observer.enabled = False
        while True:
            try:
                observer.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                observer.queue.task_done()
        task = observer.task
        if task is None or task.done():
            return
        observer.queue.put_nowait(None)
        if task is not asyncio.current_task():
            await task

    async def _run_sink_observer(self, observer: _SinkObserver) -> None:
        while True:
            event = await observer.queue.get()
            try:
                if event is None:
                    return
                if not observer.enabled:
                    continue
                try:
                    await asyncio.wait_for(
                        _invoke_sink(observer.callback, event),
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
                observer.queue.task_done()

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
        await self._record_event(
            EventRecord(
                session_id=source.session_id,
                run_id=source.run_id,
                event_type=event_type,
                payload=payload,
            ),
            notify_observers=False,
        )

    async def _join_sink_queues(self) -> None:
        joins = [observer.queue.join() for observer in self._sink_observers]
        if joins:
            await asyncio.gather(*joins)


def _runtime_event_observation(event: EventRecord) -> RuntimeEventObservation:
    payload = cast("Mapping[str, FrozenJson]", freeze_json(redact_data(event.payload)))
    metadata = cast("Mapping[str, FrozenJson]", freeze_json(redact_data(event.metadata)))
    return RuntimeEventObservation(
        session_id=event.session_id,
        run_id=event.run_id,
        record=RuntimeEventView(
            event_id=event.event_id,
            session_id=event.session_id,
            run_id=event.run_id,
            turn_id=event.turn_id,
            event_type=event.event_type,
            created_at=event.created_at,
            sequence=event.sequence,
            history_epoch=event.history_epoch,
            payload=payload,
            metadata=metadata,
        ),
    )


async def _invoke_sink(callback: RuntimeEventSink, event: EventRecord) -> None:
    if iscoroutinefunction(callback):
        await callback(event)
        return
    result = await asyncio.to_thread(callback, event)
    if isawaitable(result):
        await result


def _noop_release() -> None:
    return None
