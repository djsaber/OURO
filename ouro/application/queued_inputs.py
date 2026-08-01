from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from ouro.core.base import OuroModel, new_id, utc_now
from ouro.core.message_semantics import SemanticTag
from ouro.core.messages import SkillActivationContent
from ouro.core.session_controls import SessionInputMode, SessionInputRole
from ouro.core.state import SessionRecord
from ouro.sessions import RuntimeSessionSnapshot, SessionRepository

# Durable metadata key for the FIFO input queue. Items with missing or invalid
# fields are dropped as invalid; older shapes are not completed.
QUEUED_INPUTS_KEY = "follow_up_queue"

QueuedInputMode = SessionInputMode
QueuedInputSource = Literal["user", "host", "plugin", "delegation"]


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
        if self.message_role == "user" and self.semantic_tag is not None:
            raise ValueError("user-role queued input cannot carry a semantic tag")
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
    ) -> tuple[SessionRecord, QueuedInput, int]:
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
