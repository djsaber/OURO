"""Compatibility imports for the former follow-up-only queue API."""

from ouro.application.queued_inputs import (
    QUEUED_INPUTS_KEY as FOLLOW_UP_QUEUE_KEY,
)
from ouro.application.queued_inputs import (
    QueuedInput as QueuedFollowUp,
)
from ouro.application.queued_inputs import (
    QueuedInputQueue,
)
from ouro.core.state import SessionRecord
from ouro.sessions import SessionSnapshot, SessionStore


class FollowUpQueue:
    """Backward-compatible view of the queue's original follow-up-only API."""

    def __init__(self, store: SessionStore) -> None:
        self._queue = QueuedInputQueue(store)

    def enqueue(
        self,
        *,
        snapshot: SessionSnapshot,
        prompt: str,
        title: str | None,
    ) -> tuple[SessionRecord, int]:
        session, _item, size = self._queue.enqueue(
            snapshot=snapshot,
            prompt=prompt,
            title=title,
            mode="follow_up",
        )
        return session, size

    def pop(self, snapshot: SessionSnapshot) -> QueuedFollowUp | None:
        item = self.peek(snapshot)
        if item is not None:
            self.acknowledge(snapshot, item.queue_id)
        return item

    def peek(self, snapshot: SessionSnapshot) -> QueuedFollowUp | None:
        return self._queue.peek(snapshot)

    def acknowledge(self, snapshot: SessionSnapshot, queue_id: str) -> None:
        self._queue.acknowledge(snapshot, queue_id)


__all__ = ["FOLLOW_UP_QUEUE_KEY", "FollowUpQueue", "QueuedFollowUp"]
