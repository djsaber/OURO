from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.state import SessionRecord
from ouro.sessions import SessionSnapshot, SessionStore

# The existing durable key remains authoritative so upgrades do not require an
# eager metadata migration. Items now carry their own delivery mode.
QUEUED_INPUTS_KEY = "follow_up_queue"

QueuedInputMode = Literal["follow_up", "steer"]


class QueuedInput(OuroModel):
    queue_id: str = Field(default_factory=lambda: new_id("queued"))
    prompt: str = Field(min_length=1)
    mode: QueuedInputMode = "follow_up"
    title: str | None = None
    queued_at: datetime = Field(default_factory=utc_now)


class QueuedInputQueue:
    """Durable FIFO storage for input submitted while a run is active.

    Delivery timing is selected per item. Consumers may filter by delivery mode;
    eligible items always retain their enqueue order.
    """

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def enqueue(
        self,
        *,
        snapshot: SessionSnapshot,
        prompt: str,
        mode: QueuedInputMode = "follow_up",
        title: str | None,
    ) -> tuple[SessionRecord, QueuedInput, int]:
        item = QueuedInput(prompt=prompt, mode=mode, title=title)
        metadata = dict(snapshot.session.metadata)
        queue = self._raw_queue(metadata)
        queue.append(item.model_dump(mode="python"))
        metadata[QUEUED_INPUTS_KEY] = queue
        session = snapshot.session.model_copy(
            update={"metadata": metadata, "updated_at": utc_now()}
        )
        self.store.save_session(session)
        return session, item, len(queue)

    def eligible(
        self,
        snapshot: SessionSnapshot,
        *,
        mode: QueuedInputMode | None = None,
        limit: int | None = None,
    ) -> list[QueuedInput]:
        """Return valid eligible items without acknowledging durable queue state."""

        raw_queue = self._raw_queue(snapshot.session.metadata)
        valid: list[QueuedInput] = []
        invalid_ids: list[int] = []
        for index, raw_item in enumerate(raw_queue):
            item = self._parse_item(raw_item)
            if item is None:
                invalid_ids.append(index)
                continue
            if mode is None or item.mode == mode:
                valid.append(item)
                if limit is not None and len(valid) >= limit:
                    break
        if invalid_ids:
            self._remove_indexes(snapshot, invalid_ids)
        return valid

    def peek(self, snapshot: SessionSnapshot) -> QueuedInput | None:
        items = self.eligible(snapshot, limit=1)
        return items[0] if items else None

    def acknowledge(self, snapshot: SessionSnapshot, queue_id: str) -> None:
        """Remove an item only after its user message is durably appended."""

        latest = self.store.load_session(snapshot.session.session_id, recover=False)
        metadata = dict(latest.session.metadata)
        queue = self._raw_queue(metadata)
        for index, raw_item in enumerate(queue):
            item = self._parse_item(raw_item)
            if item is not None and item.queue_id == queue_id:
                queue.pop(index)
                self._save_queue(latest.session, metadata, queue)
                return

    def _remove_indexes(self, snapshot: SessionSnapshot, indexes: list[int]) -> None:
        metadata = dict(snapshot.session.metadata)
        queue = self._raw_queue(metadata)
        invalid = set(indexes)
        queue = [item for index, item in enumerate(queue) if index not in invalid]
        self._save_queue(snapshot.session, metadata, queue)

    def _save_queue(
        self,
        session: SessionRecord,
        metadata: dict[str, object],
        queue: list[object],
    ) -> None:
        if queue:
            metadata[QUEUED_INPUTS_KEY] = queue
        else:
            metadata.pop(QUEUED_INPUTS_KEY, None)
        updated = session.model_copy(update={"metadata": metadata, "updated_at": utc_now()})
        self.store.save_session(updated)

    def _raw_queue(self, metadata: dict[str, object]) -> list[object]:
        current = metadata.get(QUEUED_INPUTS_KEY)
        return list(current) if isinstance(current, list) else []

    def _parse_item(self, item: object) -> QueuedInput | None:
        if not isinstance(item, dict):
            return None
        prompt = item.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return None
        title = item.get("title")
        raw_mode = item.get("mode", "follow_up")
        if raw_mode not in {"follow_up", "steer"}:
            return None
        return QueuedInput(
            queue_id=str(item.get("queue_id") or new_id("queued")),
            prompt=prompt,
            mode=raw_mode,
            title=title if isinstance(title, str) else None,
            queued_at=item.get("queued_at") or utc_now(),
        )
