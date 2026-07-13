from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookFailure, HookRegistry
from ouro.sessions import SessionStore, append_ordered_event

RuntimeEventSink = Callable[[EventRecord], None | Awaitable[None]]


@dataclass(frozen=True)
class _EventSinkFailure:
    error_type: str
    error_message: str


class RuntimeEventRecorder:
    """Persist runtime events and convert hook failures into diagnostic events."""

    def __init__(
        self,
        *,
        store: SessionStore,
        hooks: HookRegistry,
        event_sink: RuntimeEventSink | None = None,
    ) -> None:
        self.store = store
        self.hooks = hooks
        self._dispatch_lock = asyncio.Lock()
        self._event_sinks: list[RuntimeEventSink] = []
        if event_sink is not None:
            self.add_event_sink(event_sink)

    def add_event_sink(self, event_sink: RuntimeEventSink) -> None:
        self._event_sinks.append(event_sink)

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
        event = EventRecord(
            session_id=session_id,
            run_id=run_id,
            turn_id=(payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None),
            event_type=event_type,
            sequence=sequence + 1,
            payload=payload,
        )
        async with self._dispatch_lock:
            event = append_ordered_event(self.store, event)
            next_sequence = event.sequence
            emitted.append(event)
            sink_failures = await self._notify_event_sinks(event)
            next_sequence = self._record_event_sink_failures(
                emitted,
                next_sequence,
                event=event,
                failures=sink_failures,
            )
            hook_result = await self.hooks.dispatch(
                HookContext(
                    hook_name="on_event",
                    session_id=session_id,
                    run_id=run_id,
                    data={"event": event.model_dump(mode="json")},
                )
            )
            return await self._record_hook_failures(
                emitted,
                next_sequence,
                hook_result.failures,
            )

    async def record_hook_failures(
        self,
        emitted: list[EventRecord],
        sequence: int,
        failures: Sequence[HookFailure],
    ) -> int:
        async with self._dispatch_lock:
            return await self._record_hook_failures(emitted, sequence, failures)

    async def _record_hook_failures(
        self,
        emitted: list[EventRecord],
        sequence: int,
        failures: Sequence[HookFailure],
    ) -> int:
        for failure in failures:
            event = EventRecord(
                session_id=failure.session_id or "unknown",
                run_id=failure.run_id or "unknown",
                event_type="plugin_hook_failed",
                sequence=sequence + 1,
                payload=failure.model_dump(mode="json"),
            )
            event = append_ordered_event(self.store, event)
            sequence = event.sequence
            emitted.append(event)
            sink_failures = await self._notify_event_sinks(event)
            sequence = self._record_event_sink_failures(
                emitted,
                sequence,
                event=event,
                failures=sink_failures,
            )
        return sequence

    async def _notify_event_sinks(self, event: EventRecord) -> list[_EventSinkFailure]:
        failures: list[_EventSinkFailure] = []
        healthy_sinks: list[RuntimeEventSink] = []
        for event_sink in self._event_sinks:
            try:
                maybe_awaitable = event_sink(event)
                if isawaitable(maybe_awaitable):
                    await maybe_awaitable
            except Exception as exc:  # noqa: BLE001 - host callbacks are an isolation boundary.
                failures.append(
                    _EventSinkFailure(
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
            else:
                healthy_sinks.append(event_sink)
        self._event_sinks = healthy_sinks
        return failures

    def _record_event_sink_failures(
        self,
        emitted: list[EventRecord],
        sequence: int,
        *,
        event: EventRecord,
        failures: Sequence[_EventSinkFailure],
    ) -> int:
        for failure in failures:
            diagnostic = EventRecord(
                session_id=event.session_id,
                run_id=event.run_id,
                event_type="event_sink_failed",
                sequence=sequence + 1,
                payload={
                    "source_event_type": event.event_type,
                    "error_type": failure.error_type,
                    "error_message": failure.error_message,
                },
            )
            diagnostic = append_ordered_event(self.store, diagnostic)
            sequence = diagnostic.sequence
            emitted.append(diagnostic)
        return sequence
