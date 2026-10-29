from __future__ import annotations

import asyncio
from collections.abc import Callable

from ouro.application.contracts.base import Diagnostic
from ouro.application.contracts.sessions import SessionObservationBatch
from ouro.application.sessions.service import SessionService
from ouro.core.callbacks import run_blocking
from ouro.core.events import EventRecord
from ouro.core.session_observation import SessionObservationPosition


class SessionEventStream:
    """Share one fact follower and immediate local delivery among Session subscribers."""

    def __init__(
        self,
        sessions: SessionService,
        session_id: str,
        *,
        position: SessionObservationPosition,
        history_epoch: int,
        sequence: int = 0,
        publish: Callable[[EventRecord], None],
        publish_facts: Callable[[SessionObservationBatch], None],
        report: Callable[[Diagnostic], None],
        is_running: Callable[[], bool],
    ) -> None:
        self._sessions = sessions
        self._session_id = session_id
        self._publish = publish
        self._publish_facts = publish_facts
        self._report = report
        self._is_running = is_running
        self._position = position
        self._history_epoch = history_epoch
        self._sequence = sequence
        self._required_message_sequence = position.message_sequence
        self._wake = asyncio.Event()
        self._closed = False

    def close(self) -> None:
        self._closed = True
        self._wake.set()

    def offer(self, event: EventRecord) -> None:
        if self._closed or event.history_epoch < self._history_epoch:
            return
        if event.commit_revision is not None and event.commit_revision <= self._position.revision:
            return
        if event.history_epoch > self._history_epoch:
            # Deliver invalidation immediately; let the shared reader establish the
            # new history's position before confirming further local commits.
            self._sequence += 1
            self._publish(event.model_copy(update={"sequence": self._sequence}))
            self._wake.set()
            return
        if event.message_after is not None:
            self._required_message_sequence = max(
                self._required_message_sequence, event.message_after
            )
        if self._required_message_sequence > self._position.message_sequence:
            self._wake.set()
            return
        if event.stream is not None and event.stream.cursor is not None:
            previous = self._position.draft
            cursor = (
                previous.cursor
                if previous and previous.context_id == event.stream.context_id
                else 0
            )
            if event.stream.cursor <= (cursor or 0):
                return
            if event.stream.after_cursor != cursor:
                self._wake.set()
                return
            self._position = self._position.model_copy(update={"draft": event.stream})
        if event.position is not None:
            if (
                event.position.revision == self._position.revision + 1
                and self._position.facts is None
            ):
                self._position = event.position.model_copy(update={"draft": self._position.draft})
            else:
                self._position = self._position.model_copy(
                    update={
                        "message_sequence": event.position.message_sequence,
                    }
                )
                self._wake.set()
        self._sequence += 1
        self._publish(event.model_copy(update={"sequence": self._sequence}))

    async def follow(self) -> None:
        try:
            while not self._closed:
                self._wake.clear()
                after = self._position
                batch, more = await run_blocking(
                    self._sessions.observation_changes,
                    self._session_id,
                    after=after,
                    history_epoch=self._history_epoch,
                    limit=128,
                )
                if self._closed:
                    return
                if (
                    after.revision != self._position.revision
                    or after.facts != self._position.facts
                    or after.message_sequence != self._position.message_sequence
                ):
                    # Local publication won the read race. Its next read starts at the
                    # newly applied position; stale pages must not replace live content.
                    continue
                local_draft = self._position.draft
                page_draft = batch.position.draft
                if (
                    local_draft is not None
                    and local_draft != after.draft
                    and (
                        page_draft is None
                        or local_draft.context_id != page_draft.context_id
                        or (local_draft.cursor or 0) > (page_draft.cursor or 0)
                    )
                ):
                    batch = batch.model_copy(
                        update={
                            "after": self._position,
                            "position": batch.position.model_copy(update={"draft": local_draft}),
                        }
                    )
                if batch.history_epoch != self._history_epoch:
                    self._required_message_sequence = batch.position.message_sequence
                self._position = batch.position
                self._history_epoch = batch.history_epoch
                if batch.facts is not None or batch.messages or batch.events:
                    self._publish_facts(batch)
                if more:
                    continue
                try:
                    async with asyncio.timeout(0.1 if self._is_running() else 0.5):
                        await self._wake.wait()
                except TimeoutError:
                    pass
        except Exception as exc:  # noqa: BLE001 - isolate this Session's reader.
            if not self._closed:
                self._report(
                    Diagnostic(
                        code="runtime_subscription_read_failed",
                        severity="error",
                        component="runtime.supervisor",
                        session_id=self._session_id,
                        message=f"Cannot follow Session facts ({type(exc).__name__}).",
                        recoverable=True,
                        suggested_action="Reopen the Session observation to retry.",
                    )
                )
        finally:
            self.close()


__all__ = ["SessionEventStream"]
