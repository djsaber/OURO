from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from ouro.application.dto import ApplicationDTO
from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.message_semantics import SemanticTag
from ouro.core.messages import SkillActivationContent
from ouro.core.session_controls import SessionInputMode, SessionInputRequest, SessionInputRole
from ouro.core.state import SessionRecord
from ouro.sessions import RuntimeSessionSnapshot, SessionRepository

# Durable metadata key for the FIFO input queue. Items with missing or invalid
# fields are dropped as invalid; older shapes are not completed.
QUEUED_INPUTS_KEY = "follow_up_queue"

QueuedInputMode = SessionInputMode
QueuedInputSource = Literal["user", "host", "plugin", "delegation"]


class QueuedUserInputView(ApplicationDTO):
    """Host-safe view of one direct user input awaiting a consumption boundary."""

    queue_id: str
    prompt: str
    mode: QueuedInputMode
    queued_at: datetime
    skill_name: str | None = None


class QueuedInput(OuroModel):
    queue_id: str = Field(default_factory=lambda: new_id("queued"))
    prompt: str = ""
    skill_activation: SkillActivationContent | None = None
    mode: QueuedInputMode = "follow_up"
    title: str | None = None
    resource_refs: tuple[str, ...] = ()
    queued_at: datetime = Field(default_factory=utc_now)
    source: QueuedInputSource = "user"
    message_role: SessionInputRole = "user"
    semantic_tag: SemanticTag | None = None
    batch_id: str | None = None
    batch_index: int | None = Field(default=None, ge=0)
    batch_size: int | None = Field(default=None, ge=1)
    coalesce_key: str | None = Field(default=None, min_length=1, max_length=128)
    host_source_id: str | None = None
    delegation_id: str | None = None
    plugin_id: str | None = None
    contribution_id: str | None = None
    generation_id: str | None = None
    execution_contract_hash: str | None = None
    caused_by_run_id: str | None = None
    continuation_decision_id: str | None = None
    session_control_controller_id: str | None = None
    session_control_activation_id: str | None = None
    prompt_policy_checked: bool = False
    prospective_run_id: str | None = None
    extension_catalog_id: str | None = None
    extension_generations: tuple[dict[str, object], ...] = ()

    @field_validator("host_source_id")
    @classmethod
    def normalize_host_source_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="before")
    @classmethod
    def default_legacy_message_role(cls, value: Any) -> Any:
        if isinstance(value, dict) and "message_role" not in value:
            normalized = dict(value)
            normalized["message_role"] = (
                "user"
                if normalized.get("source", "user") in {"user", "delegation"}
                else "system_synthetic"
            )
            return normalized
        return value

    @model_validator(mode="after")
    def validate_source(self) -> QueuedInput:
        plugin_attribution = (
            self.plugin_id,
            self.contribution_id,
            self.generation_id,
            self.execution_contract_hash,
            self.continuation_decision_id,
            self.session_control_controller_id,
            self.session_control_activation_id,
        )
        if self.source == "delegation" and not self.delegation_id:
            raise ValueError("delegated task input requires a delegation id")
        if self.source != "delegation" and self.delegation_id is not None:
            raise ValueError("only delegated task input can carry a delegation id")
        if self.source == "plugin" and (
            not self.plugin_id
            or not self.contribution_id
            or not self.generation_id
            or not self.execution_contract_hash
        ):
            raise ValueError("plugin queued input requires complete extension attribution")
        if self.source == "plugin" and self.host_source_id is not None:
            raise ValueError("plugin queued input cannot carry Host attribution")
        if self.source == "delegation" and (
            self.host_source_id is not None
            or any(value is not None for value in plugin_attribution)
        ):
            raise ValueError("delegated task input cannot carry Host or plugin attribution")
        if self.source == "host":
            if not self.host_source_id:
                raise ValueError("Host queued input requires a source id")
            if any(value is not None for value in plugin_attribution):
                raise ValueError("Host queued input cannot carry plugin attribution")
        if self.source == "user" and (
            self.host_source_id is not None
            or any(value is not None for value in plugin_attribution)
        ):
            raise ValueError("user queued input cannot carry synthetic-source attribution")
        if self.source in {"user", "delegation"} and self.message_role != "user":
            raise ValueError(f"{self.source} queued input must use the user message role")
        if self.source == "host" and self.message_role != "system_synthetic":
            raise ValueError("Host queued input must use the synthetic message role")
        batch_fields = (self.batch_id, self.batch_index, self.batch_size)
        if any(value is not None for value in batch_fields) and any(
            value is None for value in batch_fields
        ):
            raise ValueError("queued input batch identity requires id, index, and size")
        if (
            self.batch_id is not None
            and self.batch_index is not None
            and self.batch_size is not None
        ):
            if self.batch_index >= self.batch_size:
                raise ValueError("queued input batch index must be smaller than its size")
        if self.coalesce_key is not None and self.batch_id is None:
            raise ValueError("coalescing is only valid for an atomic input unit")
        if self.source == "plugin" and self.resource_refs:
            raise ValueError("plugin queued input cannot carry Host resource references")
        if (self.session_control_controller_id is None) is not (
            self.session_control_activation_id is None
        ):
            raise ValueError(
                "queued input Session control binding requires controller and activation ids"
            )
        if self.prompt_policy_checked and self.source != "user":
            raise ValueError("only direct user input can be prompt-policy checked")
        if self.prompt_policy_checked and not self.prospective_run_id:
            raise ValueError("prompt-policy checked input requires a prospective run id")
        if self.extension_generations and not self.extension_catalog_id:
            raise ValueError("queued extension generations require a catalog id")
        if not self.prompt.strip() and self.skill_activation is None:
            raise ValueError("queued input requires a prompt or skill activation")
        if self.skill_activation is not None:
            if self.source != "user" or self.message_role != "user":
                raise ValueError("skill activation is only valid on direct user input")
            if self.mode != "follow_up":
                raise ValueError("skill activation cannot steer an active run")
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
        skill_activation: SkillActivationContent | None = None,
        mode: QueuedInputMode = "follow_up",
        title: str | None,
        resource_refs: tuple[str, ...] = (),
        queue_id: str | None = None,
        source: QueuedInputSource = "user",
        message_role: SessionInputRole | None = None,
        semantic_tag: SemanticTag | None = None,
        host_source_id: str | None = None,
        delegation_id: str | None = None,
        plugin_id: str | None = None,
        contribution_id: str | None = None,
        generation_id: str | None = None,
        execution_contract_hash: str | None = None,
        caused_by_run_id: str | None = None,
        continuation_decision_id: str | None = None,
        session_control_controller_id: str | None = None,
        session_control_activation_id: str | None = None,
        prompt_policy_checked: bool = False,
        prospective_run_id: str | None = None,
        extension_catalog_id: str | None = None,
        extension_generations: tuple[dict[str, object], ...] = (),
        coalesce_key: str | None = None,
    ) -> tuple[SessionRecord, QueuedInput, int]:
        batch_id = new_id("batch") if coalesce_key is not None else None
        item = QueuedInput(
            queue_id=queue_id or new_id("queued"),
            prompt=prompt,
            skill_activation=skill_activation,
            mode=mode,
            title=title,
            resource_refs=resource_refs,
            source=source,
            message_role=(
                message_role
                if message_role is not None
                else "user"
                if source in {"user", "delegation"}
                else "system_synthetic"
            ),
            semantic_tag=semantic_tag,
            batch_id=batch_id,
            batch_index=0 if batch_id is not None else None,
            batch_size=1 if batch_id is not None else None,
            coalesce_key=coalesce_key,
            host_source_id=host_source_id,
            delegation_id=delegation_id,
            plugin_id=plugin_id,
            contribution_id=contribution_id,
            generation_id=generation_id,
            execution_contract_hash=execution_contract_hash,
            caused_by_run_id=caused_by_run_id,
            continuation_decision_id=continuation_decision_id,
            session_control_controller_id=session_control_controller_id,
            session_control_activation_id=session_control_activation_id,
            prompt_policy_checked=prompt_policy_checked,
            prospective_run_id=prospective_run_id,
            extension_catalog_id=extension_catalog_id,
            extension_generations=extension_generations,
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

    def enqueue_batch(
        self,
        *,
        snapshot: RuntimeSessionSnapshot,
        inputs: tuple[SessionInputRequest, ...],
        source: QueuedInputSource,
        plugin_id: str,
        contribution_id: str,
        generation_id: str,
        execution_contract_hash: str,
        coalesce_key: str | None = None,
        caused_by_run_id: str | None = None,
        session_control_controller_id: str | None = None,
        session_control_activation_id: str | None = None,
    ) -> tuple[SessionRecord, tuple[QueuedInput, ...], int, tuple[QueuedInput, ...]]:
        """Atomically enqueue one ordered input unit and supersede its pending projection."""

        if not inputs:
            raise ValueError("queued input batch cannot be empty")
        if len({item.mode for item in inputs}) != 1:
            raise ValueError("all inputs in a queued batch must use the same mode")
        batch_id = new_id("batch")
        queued_items = tuple(
            QueuedInput(
                prompt=item.prompt,
                mode=item.mode,
                title=item.title,
                source=source,
                message_role=item.role,
                semantic_tag=item.semantic_tag,
                batch_id=batch_id,
                batch_index=index,
                batch_size=len(inputs),
                coalesce_key=coalesce_key,
                plugin_id=plugin_id,
                contribution_id=contribution_id,
                generation_id=generation_id,
                execution_contract_hash=execution_contract_hash,
                caused_by_run_id=caused_by_run_id,
                session_control_controller_id=session_control_controller_id,
                session_control_activation_id=session_control_activation_id,
            )
            for index, item in enumerate(inputs)
        )
        with self.store.session_mutation(snapshot.session.session_id):
            latest = self.store.load_session_record(snapshot.session.session_id)
            metadata = dict(latest.metadata)
            queue = self._raw_queue(metadata)
            superseded: list[QueuedInput] = []
            retained: list[object] = []
            for raw_item in queue:
                existing = self._parse_item(raw_item)
                if (
                    coalesce_key is not None
                    and existing is not None
                    and existing.source == source
                    and existing.plugin_id == plugin_id
                    and existing.contribution_id == contribution_id
                    and existing.coalesce_key == coalesce_key
                ):
                    superseded.append(existing)
                else:
                    retained.append(raw_item)
            retained.extend(item.model_dump(mode="python") for item in queued_items)
            metadata[QUEUED_INPUTS_KEY] = retained
            session = latest.model_copy(update={"metadata": metadata, "updated_at": utc_now()})
            self.store.save_session(session)
        return session, queued_items, len(retained), tuple(superseded)

    def eligible(
        self,
        snapshot: RuntimeSessionSnapshot,
        *,
        mode: QueuedInputMode | None = None,
        limit: int | None = None,
    ) -> list[QueuedInput]:
        """Return valid eligible items without acknowledging durable queue state."""

        return self.eligible_session(snapshot.session, mode=mode, limit=limit)

    def eligible_session(
        self,
        session: SessionRecord,
        *,
        mode: QueuedInputMode | None = None,
        limit: int | None = None,
    ) -> list[QueuedInput]:
        """Read queue metadata without loading unrelated messages or Run records."""

        raw_queue = self._raw_queue(session.metadata)
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
            self._remove_invalid(session.session_id)
        return valid

    def eligible_units(
        self,
        snapshot: RuntimeSessionSnapshot,
        *,
        mode: QueuedInputMode | None = None,
    ) -> list[tuple[QueuedInput, ...]]:
        """Return complete delivery units without splitting an atomic batch."""

        return self.eligible_units_session(snapshot.session, mode=mode)

    def eligible_units_session(
        self,
        session: SessionRecord,
        *,
        mode: QueuedInputMode | None = None,
    ) -> list[tuple[QueuedInput, ...]]:
        items = self.eligible_session(session, mode=mode)
        units: list[tuple[QueuedInput, ...]] = []
        index = 0
        while index < len(items):
            item = items[index]
            if item.batch_id is None:
                units.append((item,))
                index += 1
                continue
            assert item.batch_size is not None
            candidate = tuple(items[index : index + item.batch_size])
            if (
                len(candidate) != item.batch_size
                or any(member.batch_id != item.batch_id for member in candidate)
                or [member.batch_index for member in candidate] != list(range(item.batch_size))
            ):
                index += 1
                continue
            units.append(candidate)
            index += item.batch_size
        return units

    def peek(self, snapshot: RuntimeSessionSnapshot) -> QueuedInput | None:
        items = self.eligible(snapshot, limit=1)
        return items[0] if items else None

    def find_session(self, session: SessionRecord, queue_id: str) -> QueuedInput | None:
        """Return one valid queued item by its stable delivery identity."""

        return next(
            (item for item in self.eligible_session(session) if item.queue_id == queue_id),
            None,
        )

    def cancel_user_input(self, *, session_id: str, queue_id: str) -> QueuedInput | None:
        """Atomically remove a direct user input that is still queued."""

        with self.store.session_mutation(session_id):
            latest = self.store.load_session_record(session_id)
            metadata = dict(latest.metadata)
            queue = self._raw_queue(metadata)
            for index, raw_item in enumerate(queue):
                item = self._parse_item(raw_item)
                if item is None or item.queue_id != queue_id or item.source != "user":
                    continue
                queue.pop(index)
                self._save_queue(latest, metadata, queue)
                return item
        return None

    def acknowledge(self, snapshot: RuntimeSessionSnapshot, queue_id: str) -> None:
        """Remove an item only after its canonical message is durably appended."""

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

    def acknowledge_many(self, *, session_id: str, queue_ids: tuple[str, ...]) -> None:
        """Atomically remove a complete delivery unit after all messages are durable."""

        selected = set(queue_ids)
        if not selected:
            return
        with self.store.session_mutation(session_id):
            latest = self.store.load_session_record(session_id)
            metadata = dict(latest.metadata)
            queue = self._raw_queue(metadata)
            retained = []
            removed: set[str] = set()
            for raw_item in queue:
                item = self._parse_item(raw_item)
                if item is not None and item.queue_id in selected:
                    removed.add(item.queue_id)
                else:
                    retained.append(raw_item)
            if removed:
                self._save_queue(latest, metadata, retained)

    def acknowledge_batch(self, *, session_id: str, batch_id: str) -> None:
        """Atomically acknowledge every still-queued member of one batch."""

        with self.store.session_mutation(session_id):
            latest = self.store.load_session_record(session_id)
            metadata = dict(latest.metadata)
            queue = self._raw_queue(metadata)
            retained = []
            removed = False
            for raw_item in queue:
                item = self._parse_item(raw_item)
                if item is not None and item.batch_id == batch_id:
                    removed = True
                else:
                    retained.append(raw_item)
            if removed:
                self._save_queue(latest, metadata, retained)

    def cancel_for_session_control(
        self,
        *,
        session_id: str,
        controller_id: str,
        activation_id: str,
    ) -> tuple[str, ...]:
        cancelled: list[str] = []
        with self.store.session_mutation(session_id):
            latest = self.store.load_session_record(session_id)
            metadata = dict(latest.metadata)
            retained: list[object] = []
            for raw_item in self._raw_queue(metadata):
                item = self._parse_item(raw_item)
                if item is not None and (
                    item.session_control_controller_id == controller_id
                    and item.session_control_activation_id == activation_id
                ):
                    cancelled.append(item.queue_id)
                else:
                    retained.append(raw_item)
            if cancelled:
                self._save_queue(latest, metadata, retained)
        return tuple(cancelled)

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
        if not isinstance(prompt, str):
            return None
        raw_skill_activation = item.get("skill_activation")
        try:
            skill_activation = (
                SkillActivationContent.model_validate(raw_skill_activation)
                if isinstance(raw_skill_activation, dict)
                else None
            )
        except ValidationError:
            return None
        if not prompt.strip() and skill_activation is None:
            return None
        title = item.get("title")
        raw_mode = item.get("mode")
        if raw_mode not in {"follow_up", "steer"}:
            return None
        raw_source = item.get("source")
        if raw_source not in {"user", "host", "plugin", "delegation"}:
            return None
        if "message_role" in item and item.get("message_role") not in {
            "user",
            "system_synthetic",
        }:
            return None
        if raw_source == "plugin" and not all(
            isinstance(item.get(field), str) and bool(item.get(field))
            for field in (
                "plugin_id",
                "contribution_id",
                "generation_id",
                "execution_contract_hash",
            )
        ):
            return None
        if raw_source == "host" and not (
            isinstance(item.get("host_source_id"), str) and bool(item.get("host_source_id"))
        ):
            return None
        if raw_source == "delegation" and not (
            isinstance(item.get("delegation_id"), str) and bool(item.get("delegation_id"))
        ):
            return None
        raw_resource_refs = item.get("resource_refs", ())
        resource_refs = (
            tuple(value for value in raw_resource_refs if isinstance(value, str))
            if isinstance(raw_resource_refs, list | tuple)
            else ()
        )
        try:
            return QueuedInput(
                queue_id=str(item.get("queue_id") or new_id("queued")),
                prompt=prompt,
                skill_activation=skill_activation,
                mode=raw_mode,
                title=title if isinstance(title, str) else None,
                resource_refs=resource_refs,
                queued_at=item.get("queued_at") or utc_now(),
                source=raw_source,
                message_role=_parse_message_role(item.get("message_role"), raw_source),
                semantic_tag=(
                    item.get("semantic_tag") if isinstance(item.get("semantic_tag"), str) else None
                ),
                batch_id=(item.get("batch_id") if isinstance(item.get("batch_id"), str) else None),
                batch_index=(
                    item.get("batch_index") if isinstance(item.get("batch_index"), int) else None
                ),
                batch_size=(
                    item.get("batch_size") if isinstance(item.get("batch_size"), int) else None
                ),
                coalesce_key=(
                    item.get("coalesce_key") if isinstance(item.get("coalesce_key"), str) else None
                ),
                host_source_id=(
                    item.get("host_source_id")
                    if isinstance(item.get("host_source_id"), str)
                    else None
                ),
                delegation_id=(
                    item.get("delegation_id")
                    if isinstance(item.get("delegation_id"), str)
                    else None
                ),
                plugin_id=(
                    item.get("plugin_id") if isinstance(item.get("plugin_id"), str) else None
                ),
                contribution_id=(
                    item.get("contribution_id")
                    if isinstance(item.get("contribution_id"), str)
                    else None
                ),
                generation_id=(
                    item.get("generation_id")
                    if isinstance(item.get("generation_id"), str)
                    else None
                ),
                execution_contract_hash=(
                    item.get("execution_contract_hash")
                    if isinstance(item.get("execution_contract_hash"), str)
                    else None
                ),
                prompt_policy_checked=item.get("prompt_policy_checked") is True,
                prospective_run_id=(
                    item.get("prospective_run_id")
                    if isinstance(item.get("prospective_run_id"), str)
                    else None
                ),
                extension_catalog_id=(
                    item.get("extension_catalog_id")
                    if isinstance(item.get("extension_catalog_id"), str)
                    else None
                ),
                extension_generations=(
                    tuple(
                        value
                        for value in item.get("extension_generations", ())
                        if isinstance(value, dict)
                    )
                    if isinstance(item.get("extension_generations", ()), list | tuple)
                    else ()
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
                session_control_controller_id=(
                    item.get("session_control_controller_id")
                    if isinstance(item.get("session_control_controller_id"), str)
                    else None
                ),
                session_control_activation_id=(
                    item.get("session_control_activation_id")
                    if isinstance(item.get("session_control_activation_id"), str)
                    else None
                ),
            )
        except ValidationError:
            return None


def _parse_message_role(value: object, source: QueuedInputSource) -> SessionInputRole:
    if value == "user":
        return "user"
    if value == "system_synthetic":
        return "system_synthetic"
    return "user" if source in {"user", "delegation"} else "system_synthetic"
