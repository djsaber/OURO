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
from ouro.core.session_observation import SessionObservationPosition

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
        position=event.position,
        commit_revision=event.commit_revision,
        message_after=event.message_after,
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
    """One applied position for shared delivery and bounded fact catch-up."""

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
        self._position = SessionObservationPosition()
        self._history_epoch = snapshot.session.history_epoch
        self._pending: SessionObservationBatch | None = self._snapshot_batch(snapshot)
        self._catching_up = False
        self._next_notification: RuntimeNotification | None = None
        self._read_lock = asyncio.Lock()
        self.closed = False

    @property
    def applied_position(self) -> SessionObservationPosition:
        return self._position

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
            except StopAsyncIteration:
                self.close()
                raise
            except asyncio.CancelledError:
                self._catching_up = True
                raise
            except Exception as exc:  # noqa: BLE001 - isolate this consumer's read failure.
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
        if self.closed or batch is not self._pending:
            raise ValueError("Only this observation's pending batch can be acknowledged")
        self._position = batch.position
        self._history_epoch = batch.history_epoch
        self._pending = None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._pending = None
            self._source.close()

    def retain_history_from(self, anchor: SessionHistoryAnchor) -> None:
        if anchor.history_epoch != self._history_epoch:
            raise ValueError("history anchor belongs to another history epoch")
        self._history_anchor = anchor

    def _snapshot_batch(self, snapshot: SessionObservationSnapshot) -> SessionObservationBatch:
        return SessionObservationBatch(
            session_id=self.session_id,
            history_epoch=snapshot.session.history_epoch,
            snapshot=snapshot,
            position=snapshot.position,
        )

    def _diagnostic_batch(self, diagnostic: Diagnostic) -> SessionObservationBatch:
        return SessionObservationBatch(
            session_id=self.session_id,
            history_epoch=self._history_epoch,
            diagnostic=diagnostic,
            position=self._position,
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
                batch, self._catching_up = await run_blocking(
                    self._sessions.observation_changes,
                    self.session_id,
                    after=self._position,
                    history_epoch=self._history_epoch,
                    limit=self._page_size,
                )
                if batch.history_epoch != self._history_epoch:
                    return await self._reset()
                if batch.facts is not None or batch.messages or batch.events:
                    return batch
            notification = self._next_notification or await self._source.get()
            self._next_notification = None
            if notification.facts is not None:
                batch = notification.facts
                if batch.history_epoch != self._history_epoch:
                    return await self._reset()
                if batch.after == self._position:
                    return batch
                if batch.position != self._position:
                    self._catching_up = True
                continue
            events: list[RuntimeEventView] = []
            position = self._position
            while True:
                if notification.diagnostic is not None:
                    if notification.diagnostic.code != "runtime_subscription_lagged":
                        return self._diagnostic_batch(notification.diagnostic)
                    self._catching_up = True
                    break
                event = notification.event
                if event is not None:
                    if event.history_epoch > self._history_epoch:
                        return await self._reset()
                    covered = event.history_epoch < self._history_epoch or (
                        event.commit_revision is not None
                        and event.commit_revision <= position.revision
                    )
                    if (
                        event.message_after is not None
                        and event.message_after > position.message_sequence
                    ):
                        self._catching_up = True
                        covered = True
                    stream = event.stream
                    if stream is not None and stream.cursor is not None:
                        previous = position.draft
                        covered = covered or (
                            previous is not None
                            and previous.context_id == stream.context_id
                            and stream.cursor <= (previous.cursor or 0)
                        )
                        if not covered:
                            position = position.model_copy(update={"draft": stream})
                    if not covered:
                        events.append(event)
                        if event.position is not None:
                            if (
                                event.position.revision == position.revision + 1
                                and position.facts is None
                            ):
                                position = event.position.model_copy(
                                    update={"draft": position.draft}
                                )
                            else:
                                position = position.model_copy(
                                    update={
                                        "message_sequence": event.position.message_sequence,
                                    }
                                )
                                self._catching_up = True
                if len(events) >= self._page_size:
                    break
                try:
                    notification = self._source.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if notification.facts is not None:
                    self._next_notification = notification
                    break
            if not events:
                continue
            ids = tuple(
                dict.fromkeys(
                    identity
                    for event in events
                    if event.event_type in _INPUT_EVENTS
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
                events=tuple(events),
                messages=messages,
                after=self._position,
                position=position,
            )
        raise StopAsyncIteration
