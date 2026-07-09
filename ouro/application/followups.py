from __future__ import annotations

from pydantic import Field

from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.state import SessionRecord
from ouro.sessions import SessionSnapshot, SessionStore

FOLLOW_UP_QUEUE_KEY = "follow_up_queue"


class QueuedFollowUp(OuroModel):
    queue_id: str = Field(default_factory=lambda: new_id("queued"))
    prompt: str
    title: str | None = None
    queued_at: object = Field(default_factory=utc_now)


class FollowUpQueue:
    """Persistent queue for user input received while a run is already active."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def enqueue(
        self,
        *,
        snapshot: SessionSnapshot,
        prompt: str,
        title: str | None,
    ) -> tuple[SessionRecord, int]:
        item = QueuedFollowUp(prompt=prompt, title=title)
        metadata = dict(snapshot.session.metadata)
        queue = list(metadata.get(FOLLOW_UP_QUEUE_KEY) or [])
        queue.append(item.model_dump(mode="python"))
        metadata[FOLLOW_UP_QUEUE_KEY] = queue
        session = snapshot.session.model_copy(
            update={"metadata": metadata, "updated_at": utc_now()}
        )
        self.store.save_session(session)
        return session, len(queue)

    def pop(self, snapshot: SessionSnapshot) -> QueuedFollowUp | None:
        metadata = dict(snapshot.session.metadata)
        queue = list(metadata.get(FOLLOW_UP_QUEUE_KEY) or [])
        if not queue:
            return None

        item = queue.pop(0)
        if queue:
            metadata[FOLLOW_UP_QUEUE_KEY] = queue
        else:
            metadata.pop(FOLLOW_UP_QUEUE_KEY, None)
        session = snapshot.session.model_copy(
            update={"metadata": metadata, "updated_at": utc_now()}
        )
        self.store.save_session(session)

        return self._parse_item(item)

    def _parse_item(self, item: object) -> QueuedFollowUp | None:
        if not isinstance(item, dict):
            return None
        prompt = item.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return None
        title = item.get("title")
        return QueuedFollowUp(
            queue_id=str(item.get("queue_id") or new_id("queued")),
            prompt=prompt,
            title=title if isinstance(title, str) else None,
            queued_at=item.get("queued_at") or utc_now(),
        )
