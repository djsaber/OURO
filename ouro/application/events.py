from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from inspect import isawaitable
from typing import Any

from ouro.core.events import EventRecord
from ouro.core.hooks import HookContext, HookFailure, HookRegistry
from ouro.sessions import SessionStore

RuntimeEventSink = Callable[[EventRecord], None | Awaitable[None]]


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
        next_sequence = sequence + 1
        event = EventRecord(
            session_id=session_id,
            run_id=run_id,
            event_type=event_type,
            sequence=next_sequence,
            payload=payload,
        )
        self.store.append_event(event)
        emitted.append(event)
        await self._notify_event_sinks(event)
        hook_result = await self.hooks.dispatch(
            HookContext(
                hook_name="on_event",
                session_id=session_id,
                run_id=run_id,
                data={"event": event.model_dump(mode="json")},
            )
        )
        return await self.record_hook_failures(emitted, next_sequence, hook_result.failures)

    async def record_hook_failures(
        self,
        emitted: list[EventRecord],
        sequence: int,
        failures: Sequence[HookFailure],
    ) -> int:
        for failure in failures:
            sequence += 1
            event = EventRecord(
                session_id=failure.session_id or "unknown",
                run_id=failure.run_id or "unknown",
                event_type="plugin_hook_failed",
                sequence=sequence,
                payload=failure.model_dump(mode="json"),
            )
            self.store.append_event(event)
            emitted.append(event)
            await self._notify_event_sinks(event)
        return sequence

    async def _notify_event_sinks(self, event: EventRecord) -> None:
        for event_sink in self._event_sinks:
            maybe_awaitable = event_sink(event)
            if isawaitable(maybe_awaitable):
                await maybe_awaitable
