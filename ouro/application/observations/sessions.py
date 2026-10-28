from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

from ouro.application.contracts.base import Diagnostic
from ouro.application.contracts.events import RuntimeEventView, project_runtime_event
from ouro.application.contracts.execution import RuntimeNotification
from ouro.application.contracts.sessions import (
    SessionHistoryAnchor,
    SessionObservationBatch,
    SessionObservationSnapshot,
)
from ouro.application.sessions.service import SessionService
from ouro.core.callbacks import run_blocking
from ouro.core.events import EventRecord
from ouro.core.messages import MessageRecord
from ouro.core.response_drafts import ResponseStreamPosition

_INPUT_EVENTS = frozenset(
    {"user_message_added", "host_message_added", "plugin_message_added", "queued_input_consumed"}
)


class ObservationSource(Protocol):
    async def get(self) -> RuntimeNotification: ...

    def get_nowait(self) -> RuntimeNotification: ...

    def close(self) -> None: ...


class ObservationSnapshotReader(Protocol):
    def __call__(
        self, session_id: str, *, history_anchor: SessionHistoryAnchor | None = None
    ) -> SessionObservationSnapshot: ...


def observation_event(event: RuntimeEventView) -> EventRecord:
    """Adapt a redacted Host event for the shared content reducer."""
    return EventRecord(
        history_epoch=event.history_epoch,
        event_id=event.event_id,
        session_id=event.session_id,
        run_id=event.run_id,
        turn_id=event.turn_id,
        event_type=event.event_type,
        created_at=event.created_at,
        sequence=event.sequence,
        stream=event.stream,
        payload=dict(event.data),
    )


def redact_observation(snapshot: SessionObservationSnapshot) -> SessionObservationSnapshot:
    return snapshot.model_copy(
        update={
            "events": tuple(
                observation_event(project_runtime_event(event)) for event in snapshot.events
            )
        }
    )


class SessionObservation(AsyncIterator[SessionObservationBatch]):
    """Snapshot and independent durable catch-up, confirmed only after content application.

    ``get`` retains one pending batch until ``acknowledge`` confirms successful
    application. Reading, buffering and rendering do not implicitly confirm it.
    The live source is only an acceleration cache; overflow starts bounded replay
    from this consumer's applied cursor rather than truncating its history.
    """

    def __init__(
        self,
        snapshot: SessionObservationSnapshot,
        source: ObservationSource,
        *,
        sessions: SessionService,
        read_snapshot: ObservationSnapshotReader,
        page_size: int,
        history_anchor: SessionHistoryAnchor | None = None,
    ) -> None:
        self.session_id = snapshot.session.session_id
        self.initial_snapshot = snapshot
        self._source = source
        self._sessions = sessions
        self._read_snapshot = read_snapshot
        self._page_size = page_size
        self._history_anchor = history_anchor
        self._pending: SessionObservationBatch | None = self._snapshot_batch(snapshot)
        self._applied_sequence = 0
        self._draft_position: ResponseStreamPosition | None = None
        self._history_epoch = snapshot.session.history_epoch
        self._catching_up = False
        self._read_lock = asyncio.Lock()
        self.closed = False

    @property
    def applied_sequence(self) -> int:
        return self._applied_sequence

    def __aiter__(self) -> SessionObservation:
        return self

    async def __anext__(self) -> SessionObservationBatch:
        return await self.get()

    async def get(self) -> SessionObservationBatch:
        async with self._read_lock:
            if self.closed:
                raise StopAsyncIteration
            if self._pending is not None:
                return self._pending
            try:
                batch = await self._next_batch()
                if self.closed:
                    raise StopAsyncIteration
            except StopAsyncIteration:
                self.close()
                raise
            except asyncio.CancelledError:
                # The source may already have yielded events before a message
                # read was cancelled. Only the applied cursor is safe to resume.
                self._catching_up = True
                raise
            except Exception as exc:  # noqa: BLE001 - isolate this observation's read failure.
                batch = self._diagnostic_batch(
                    Diagnostic(
                        code="session_observation_read_failed",
                        severity="error",
                        component="application.session_observation",
                        session_id=self.session_id,
                        message=f"Cannot read Session observation ({type(exc).__name__}).",
                        recoverable=True,
                        suggested_action="Reopen the Session observation to retry.",
                    )
                )
            if batch.diagnostic is not None:
                self.close()
            else:
                self._pending = batch
            return batch

    def acknowledge(self, batch: SessionObservationBatch) -> None:
        """Confirm an exact delivered batch after its content projection succeeds."""
        if self.closed or batch is not self._pending:
            raise ValueError("Only this observation's pending batch can be acknowledged")
        self._applied_sequence = batch.event_high_water
        self._history_epoch = batch.history_epoch
        events = batch.snapshot.events if batch.snapshot is not None else batch.events
        for event in events:
            if event.stream is not None and event.stream.cursor is not None:
                self._draft_position = event.stream
        self._pending = None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._pending = None
            self._source.close()

    def retain_history_from(self, anchor: SessionHistoryAnchor) -> None:
        """Preserve a Host's loaded window across same-epoch snapshot resets."""
        if anchor.history_epoch != self._history_epoch:
            raise ValueError("history anchor belongs to another history epoch")
        self._history_anchor = anchor

    def _snapshot_batch(self, snapshot: SessionObservationSnapshot) -> SessionObservationBatch:
        return SessionObservationBatch(
            session_id=self.session_id,
            history_epoch=snapshot.session.history_epoch,
            event_high_water=snapshot.event_high_water,
            snapshot=snapshot,
        )

    def _diagnostic_batch(self, diagnostic: Diagnostic) -> SessionObservationBatch:
        return SessionObservationBatch(
            session_id=self.session_id,
            history_epoch=self._history_epoch,
            event_high_water=self._applied_sequence,
            diagnostic=diagnostic,
        )

    async def _reset(self) -> SessionObservationBatch:
        snapshot = await run_blocking(self._read_redacted_snapshot)
        self._catching_up = False
        return self._snapshot_batch(snapshot)

    def _read_redacted_snapshot(self) -> SessionObservationSnapshot:
        snapshot = (
            self._read_snapshot(self.session_id, history_anchor=self._history_anchor)
            if self._history_anchor is not None
            else self._read_snapshot(self.session_id)
        )
        return redact_observation(snapshot)

    async def _next_batch(self) -> SessionObservationBatch:
        while not self.closed:
            if self._catching_up:
                batch, more = await run_blocking(
                    self._sessions.observation_events,
                    self.session_id,
                    after_sequence=self._applied_sequence,
                    limit=self._page_size,
                    draft_position=self._draft_position,
                )
                if batch.history_epoch != self._history_epoch or any(
                    self._requires_snapshot(event) for event in batch.events
                ):
                    return await self._reset()
                self._catching_up = more
                if batch.events or batch.event_high_water > self._applied_sequence:
                    return batch
            notification = await self._source.get()
            events: list[RuntimeEventView] = []
            while True:
                if notification.diagnostic is not None:
                    if notification.diagnostic.code != "runtime_subscription_lagged":
                        return self._diagnostic_batch(notification.diagnostic)
                    # None of this batch has been applied. Re-read its complete suffix.
                    self._catching_up = True
                    break
                event = notification.event
                if event is not None and (
                    event.sequence > self._applied_sequence
                    or (
                        event.stream is not None
                        and (
                            event.stream.cursor is None
                            or self._draft_position is None
                            or event.stream.context_id != self._draft_position.context_id
                            or event.stream.cursor > (self._draft_position.cursor or 0)
                        )
                    )
                ):
                    if self._requires_snapshot(event):
                        return await self._reset()
                    events.append(event)
                if len(events) >= self._page_size:
                    break
                try:
                    notification = self._source.get_nowait()
                except asyncio.QueueEmpty:
                    break
            if self._catching_up or not events:
                continue
            ids = tuple(
                dict.fromkeys(
                    identity
                    for event in events
                    if event.history_epoch == self._history_epoch
                    and event.event_type in _INPUT_EVENTS
                    if isinstance((identity := event.data.get("message_id")), str)
                )
            )
            messages: tuple[MessageRecord, ...] = ()
            if ids:
                page = await run_blocking(
                    self._sessions.messages_at_history,
                    self.session_id,
                    ids,
                    history_epoch=self._history_epoch,
                )
                if page.history_epoch != self._history_epoch:
                    return await self._reset()
                messages = page.messages
            return SessionObservationBatch(
                session_id=self.session_id,
                history_epoch=self._history_epoch,
                event_high_water=max(self._applied_sequence, *(event.sequence for event in events)),
                events=tuple(events),
                messages=messages,
            )
        raise StopAsyncIteration

    def _requires_snapshot(self, event: RuntimeEventView) -> bool:
        return event.history_epoch > self._history_epoch or (
            event.history_epoch == self._history_epoch
            and event.event_type == "run_status_changed"
            and event.data.get("resumed") is True
        )
