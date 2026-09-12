from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from ouro.application.dto import ApplicationDTO
from ouro.application.supervisor_models import HostInputReceipt
from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import SemanticTag
from ouro.core.messages import SkillActivationContent
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.session_controls import HostSessionInput, SessionInputMode, SessionInputRequest
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
    history_epoch: int = Field(default=0, ge=0)
    history_bound: bool = False
    queue_id: str = Field(min_length=1)
    prompt: str = ""
    skill_activation: SkillActivationContent | None = None
    mode: QueuedInputMode
    title: str | None = None
    resource_refs: tuple[str, ...] = ()
    queued_at: datetime
    source: QueuedInputSource
    role: MessageRole
    scaffold: bool
    semantic_tag: SemanticTag | None
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
            raise ValueError("user queued input cannot carry Host or plugin attribution")
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
            if self.source != "user" or self.role != "user" or self.scaffold:
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

    def accept_host(
        self,
        session_id: str,
        request: HostSessionInput,
    ) -> tuple[HostInputReceipt, QueuedInput | None, SessionRecord]:
        """Atomically accept an input or return its pending/consumed receipt.

        A retry after canonical consumption must not reinsert the same input.
        Only newly accepted items are returned for event publication.
        """
        queue_id = request.queue_id or new_id("queued")
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            if request.history_epoch is not None and request.history_epoch != session.history_epoch:
                raise SessionCommitConflict("Host input belongs to withdrawn Session history")
            existing = self.find_session(session, queue_id)
            if existing is not None:
                if (
                    existing.host_source_id != request.host_source_id
                    or existing.prompt != request.prompt
                    or existing.mode != request.mode
                    or existing.resource_refs != request.resource_refs
                    or existing.role != request.role
                    or existing.scaffold != request.scaffold
                    or existing.semantic_tag != request.semantic_tag
                ):
                    raise ValueError("Host input queue id is already bound to another input")
                return (
                    HostInputReceipt(
                        session_id=session_id,
                        queue_id=queue_id,
                        accepted_at=existing.queued_at,
                    ),
                    None,
                    session,
                )
            message = self.store.find_queued_input_message(session_id, queue_id)
            if message is not None:
                if (
                    message.metadata.get("host_source_id") != request.host_source_id
                    or message.text_content != request.prompt
                    or tuple(message.resource_refs) != request.resource_refs
                    or message.metadata.get("queued_input_mode") != request.mode
                    or message.role != request.role
                    or message.scaffold != request.scaffold
                    or message.semantic_tag != request.semantic_tag
                ):
                    raise ValueError("Host input queue id is already bound to another input")
                return (
                    HostInputReceipt(
                        session_id=session_id,
                        queue_id=queue_id,
                        accepted_at=message.created_at,
                        message_id=message.message_id,
                        run_id=message.run_id,
                    ),
                    None,
                    session,
                )
            if self.store.load_commit(session_id, _input_operation_id(queue_id)) is not None:
                raise ValueError(
                    "Host input was already accepted and has been withdrawn or cancelled"
                )
            session, item, _ = self.enqueue(
                snapshot=RuntimeSessionSnapshot(session=session),
                prompt=request.prompt,
                mode=request.mode,
                title=request.title,
                resource_refs=request.resource_refs,
                queue_id=queue_id,
                source="host",
                role=request.role,
                scaffold=request.scaffold,
                semantic_tag=request.semantic_tag,
                host_source_id=request.host_source_id,
                caused_by_run_id=session.active_run_id,
                history_bound=request.history_epoch is not None,
            )
            return (
                HostInputReceipt(
                    session_id=session_id,
                    queue_id=queue_id,
                    accepted_at=item.queued_at,
                ),
                item,
                session,
            )

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
        role: MessageRole,
        scaffold: bool,
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
        history_bound: bool = False,
    ) -> tuple[SessionRecord, QueuedInput, int]:
        batch_id = new_id("batch") if coalesce_key is not None else None
        item = QueuedInput(
            history_epoch=snapshot.session.history_epoch,
            history_bound=history_bound,
            queue_id=queue_id or new_id("queued"),
            prompt=prompt,
            skill_activation=skill_activation,
            mode=mode,
            title=title,
            resource_refs=resource_refs,
            queued_at=utc_now(),
            source=source,
            role=role,
            scaffold=scaffold,
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
            if item.history_epoch != latest.history_epoch:
                raise SessionCommitConflict("Queued input belongs to withdrawn Session history")
            metadata = dict(latest.metadata)
            queue = self._raw_queue(metadata)
            for raw_item in queue:
                existing = self._parse_item(raw_item)
                if existing is not None and existing.queue_id == item.queue_id:
                    return latest, existing, len(queue)
            queue.append(item.model_dump(mode="python"))
            metadata[QUEUED_INPUTS_KEY] = queue
            session = latest.model_copy(update={"metadata": metadata, "updated_at": utc_now()})
            self.store.commit_session(
                SessionCommit(
                    operation_id=_input_operation_id(item.queue_id),
                    before=latest,
                    after=session,
                    output={"queue_id": item.queue_id},
                )
            )
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
                history_epoch=snapshot.session.history_epoch,
                queue_id=new_id("queued"),
                prompt=item.prompt,
                mode=item.mode,
                title=item.title,
                queued_at=utc_now(),
                source=source,
                role=item.role,
                scaffold=item.scaffold,
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
            session, superseded = self.stage_batch(latest, queued_items)
            self.store.save_session(session)
        return session, queued_items, len(self._raw_queue(session.metadata)), superseded

    def stage_batch(
        self,
        session: SessionRecord,
        queued_items: tuple[QueuedInput, ...],
    ) -> tuple[SessionRecord, tuple[QueuedInput, ...]]:
        """Append a prepared unit to the caller's Session draft, without saving it."""
        if any(item.history_epoch != session.history_epoch for item in queued_items):
            raise SessionCommitConflict("Queued input belongs to withdrawn Session history")
        first = queued_items[0]
        metadata = dict(session.metadata)
        superseded: list[QueuedInput] = []
        retained: list[object] = []
        for raw_item in self._raw_queue(metadata):
            existing = self._parse_item(raw_item)
            if (
                first.coalesce_key is not None
                and existing is not None
                and existing.source == first.source
                and existing.plugin_id == first.plugin_id
                and existing.contribution_id == first.contribution_id
                and existing.coalesce_key == first.coalesce_key
            ):
                superseded.append(existing)
            else:
                retained.append(raw_item)
        retained.extend(item.model_dump(mode="python") for item in queued_items)
        metadata[QUEUED_INPUTS_KEY] = retained
        return session.model_copy(update={"metadata": metadata, "updated_at": utc_now()}), tuple(
            superseded
        )

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
            if item.history_epoch != session.history_epoch:
                if item.history_bound or item.source in {"plugin", "delegation"}:
                    continue
                item = item.model_copy(
                    update={
                        "history_epoch": session.history_epoch,
                        "prompt_policy_checked": False,
                        "prospective_run_id": None,
                        "extension_catalog_id": None,
                        "extension_generations": (),
                    }
                )
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

    def without_inputs(self, session: SessionRecord, queue_ids: tuple[str, ...]) -> SessionRecord:
        """Stage exact removals for the same decision that appends their messages."""
        selected = set(queue_ids)
        metadata = dict(session.metadata)
        retained = [
            raw
            for raw in self._raw_queue(metadata)
            if not isinstance(raw, dict) or raw.get("queue_id") not in selected
        ]
        if retained:
            metadata[QUEUED_INPUTS_KEY] = retained
        else:
            metadata.pop(QUEUED_INPUTS_KEY, None)
        return session.model_copy(update={"metadata": metadata})

    def after_withdrawal(self, session: SessionRecord) -> tuple[SessionRecord, tuple[str, ...]]:
        """Keep independent accepted inputs, replacing only their obsolete execution permits."""
        retained = []
        cancelled = []
        for raw in self._raw_queue(session.metadata):
            item = self._parse_item(raw)
            if item is None:
                continue
            if item.history_bound or item.source in {"plugin", "delegation"}:
                cancelled.append(item.queue_id)
                continue
            retained.append(
                item.model_copy(
                    update={
                        "history_epoch": session.history_epoch + 1,
                        "prompt_policy_checked": False,
                        "prospective_run_id": None,
                        "extension_catalog_id": None,
                        "extension_generations": (),
                    }
                ).model_dump(mode="python")
            )
        metadata = dict(session.metadata)
        if retained:
            metadata[QUEUED_INPUTS_KEY] = retained
        else:
            metadata.pop(QUEUED_INPUTS_KEY, None)
        return session.model_copy(update={"metadata": metadata}), tuple(cancelled)

    def without_session_control(
        self,
        session: SessionRecord,
        *,
        controller_id: str,
        activation_id: str,
    ) -> tuple[SessionRecord, tuple[str, ...]]:
        """Prepare a queue removal for the caller's Session commit."""
        cancelled: list[str] = []
        metadata = dict(session.metadata)
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
        if not cancelled:
            return session, ()
        if retained:
            metadata[QUEUED_INPUTS_KEY] = retained
        else:
            metadata.pop(QUEUED_INPUTS_KEY, None)
        return session.model_copy(update={"metadata": metadata}), tuple(cancelled)

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
        try:
            return QueuedInput.model_validate(item)
        except ValidationError:
            return None


def _input_operation_id(queue_id: str) -> str:
    """Acceptance survives consumption and history withdrawal in the native receipt journal."""
    return "input_" + hashlib.sha256(queue_id.encode("utf-8")).hexdigest()
