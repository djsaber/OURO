from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.session_controls import SessionInputMode
from ouro.core.state import SessionRecord
from ouro.sessions import RuntimeSessionSnapshot, SessionRepository

# The existing durable key remains authoritative so upgrades do not require an
# eager metadata migration. Items now carry their own delivery mode.
QUEUED_INPUTS_KEY = "follow_up_queue"

QueuedInputMode = SessionInputMode
QueuedInputSource = Literal["user", "plugin"]


class QueuedInput(OuroModel):
    queue_id: str = Field(default_factory=lambda: new_id("queued"))
    prompt: str = Field(min_length=1)
    mode: QueuedInputMode = "follow_up"
    title: str | None = None
    resource_refs: tuple[str, ...] = ()
    queued_at: datetime = Field(default_factory=utc_now)
    source: QueuedInputSource = "user"
    plugin_id: str | None = None
    contribution_id: str | None = None
    generation_id: str | None = None
    caused_by_run_id: str | None = None
    continuation_decision_id: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> QueuedInput:
        if self.source == "plugin" and (
            not self.plugin_id or not self.contribution_id or not self.generation_id
        ):
            raise ValueError("plugin queued input requires complete extension attribution")
        if self.source == "user" and any(
            value is not None
            for value in (
                self.plugin_id,
                self.contribution_id,
                self.generation_id,
                self.continuation_decision_id,
            )
        ):
            raise ValueError("user queued input cannot carry plugin attribution")
        return self


class QueuedInputQueue:
    """Durable FIFO storage for input submitted while a run is active.

    Delivery timing is selected per item. Consumers may filter by delivery mode;
    eligible items always retain their enqueue order.
    """

    def __init__(self, store: SessionRepository) -> None:
        self.store = store

    def enqueue(
        self,
        *,
        snapshot: RuntimeSessionSnapshot,
        prompt: str,
        mode: QueuedInputMode = "follow_up",
        title: str | None,
        resource_refs: tuple[str, ...] = (),
        queue_id: str | None = None,
        source: QueuedInputSource = "user",
        plugin_id: str | None = None,
        contribution_id: str | None = None,
        generation_id: str | None = None,
        caused_by_run_id: str | None = None,
        continuation_decision_id: str | None = None,
    ) -> tuple[SessionRecord, QueuedInput, int]:
        item = QueuedInput(
            queue_id=queue_id or new_id("queued"),
            prompt=prompt,
            mode=mode,
            title=title,
            resource_refs=resource_refs,
            source=source,
            plugin_id=plugin_id,
            contribution_id=contribution_id,
            generation_id=generation_id,
            caused_by_run_id=caused_by_run_id,
            continuation_decision_id=continuation_decision_id,
        )
        with self.store.session_mutation(snapshot.session.session_id):
            latest = self.store.load_session_record(snapshot.session.session_id)
            metadata = dict(latest.metadata)
            queue = self._raw_queue(metadata)
            for raw_item in queue:
                existing = self._parse_item(raw_item)
                if existing is not None and existing.queue_id == item.queue_id:
                    return latest, existing, len(queue)
            queue.append(item.model_dump(mode="python"))
            metadata[QUEUED_INPUTS_KEY] = queue
            session = latest.model_copy(update={"metadata": metadata, "updated_at": utc_now()})
            self.store.save_session(session)
        return session, item, len(queue)

    def eligible(
        self,
        snapshot: RuntimeSessionSnapshot,
        *,
        mode: QueuedInputMode | None = None,
        limit: int | None = None,
    ) -> list[QueuedInput]:
        """Return valid eligible items without acknowledging durable queue state."""

        raw_queue = self._raw_queue(snapshot.session.metadata)
        valid: list[QueuedInput] = []
        saw_invalid = False
        for raw_item in raw_queue:
            item = self._parse_item(raw_item)
            if item is None:
                saw_invalid = True
                continue
            if mode is None or item.mode == mode:
                valid.append(item)
                if limit is not None and len(valid) >= limit:
                    break
        if saw_invalid:
            self._remove_invalid(snapshot.session.session_id)
        return valid

    def peek(self, snapshot: RuntimeSessionSnapshot) -> QueuedInput | None:
        items = self.eligible(snapshot, limit=1)
        return items[0] if items else None

    def acknowledge(self, snapshot: RuntimeSessionSnapshot, queue_id: str) -> None:
        """Remove an item only after its user message is durably appended."""

        with self.store.session_mutation(snapshot.session.session_id):
            latest = self.store.load_session_record(snapshot.session.session_id)
            metadata = dict(latest.metadata)
            queue = self._raw_queue(metadata)
            for index, raw_item in enumerate(queue):
                item = self._parse_item(raw_item)
                if item is not None and item.queue_id == queue_id:
                    queue.pop(index)
                    self._save_queue(latest, metadata, queue)
                    return

    def _remove_invalid(self, session_id: str) -> None:
        with self.store.session_mutation(session_id):
            latest = self.store.load_session_record(session_id)
            metadata = dict(latest.metadata)
            queue = [
                raw_item
                for raw_item in self._raw_queue(metadata)
                if self._parse_item(raw_item) is not None
            ]
            self._save_queue(latest, metadata, queue)

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
        raw_source = item.get("source", "user")
        if raw_source not in {"user", "plugin"}:
            return None
        if raw_source == "plugin" and not all(
            isinstance(item.get(field), str) and bool(item.get(field))
            for field in ("plugin_id", "contribution_id", "generation_id")
        ):
            return None
        raw_resource_refs = item.get("resource_refs", ())
        resource_refs = (
            tuple(value for value in raw_resource_refs if isinstance(value, str))
            if isinstance(raw_resource_refs, list | tuple)
            else ()
        )
        return QueuedInput(
            queue_id=str(item.get("queue_id") or new_id("queued")),
            prompt=prompt,
            mode=raw_mode,
            title=title if isinstance(title, str) else None,
            resource_refs=resource_refs,
            queued_at=item.get("queued_at") or utc_now(),
            source=raw_source,
            plugin_id=item.get("plugin_id") if isinstance(item.get("plugin_id"), str) else None,
            contribution_id=(
                item.get("contribution_id")
                if isinstance(item.get("contribution_id"), str)
                else None
            ),
            generation_id=(
                item.get("generation_id") if isinstance(item.get("generation_id"), str) else None
            ),
            caused_by_run_id=(
                item.get("caused_by_run_id")
                if isinstance(item.get("caused_by_run_id"), str)
                else None
            ),
            continuation_decision_id=(
                item.get("continuation_decision_id")
                if isinstance(item.get("continuation_decision_id"), str)
                else None
            ),
        )
