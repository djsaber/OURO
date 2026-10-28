from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from ouro.application.contracts.execution import HostInputReceipt
from ouro.application.contracts.sessions import QueuedUserInputView as QueuedUserInputView
from ouro.core.base import new_id, utc_now
from ouro.core.errors import PersistenceError
from ouro.core.events import EventRecord
from ouro.core.input_content import InputFile, InputSkillReference
from ouro.core.inputs import InputRecord, InputState, InputStateChange
from ouro.core.inputs import QueuedInput as QueuedInput
from ouro.core.inputs import QueuedInputMode as QueuedInputMode
from ouro.core.inputs import QueuedInputSource as QueuedInputSource
from ouro.core.message_roles import MessageRole
from ouro.core.message_semantics import SemanticTag
from ouro.core.messages import MessageRecord, SkillActivationContent
from ouro.core.run_drivers import RunDriverBinding
from ouro.core.session_commits import SessionCommit, SessionCommitConflict
from ouro.core.session_controls import HostSessionInput, SessionInputRequest
from ouro.core.state import RunRecord, SessionRecord
from ouro.sessions import RuntimeSessionSnapshot, SessionRepository


@dataclass(frozen=True)
class InputQueueChange:
    inputs: tuple[InputRecord, ...]
    changes: tuple[InputStateChange, ...]
    superseded: tuple[QueuedInput, ...] = ()


class QueuedInputQueue:
    """FIFO references to accepted inputs; bodies are immutable independent facts."""

    def __init__(self, store: SessionRepository) -> None:
        self.store = store

    def _state(self, session_id: str, input_id: str) -> InputState:
        state = self.store.load_input_state(session_id, input_id)
        if state is None:
            raise PersistenceError(f"accepted input state not found: {input_id}")
        return state

    def are_consumed(self, session_id: str, input_ids: Iterable[str]) -> bool:
        with self.store.session_read(session_id):
            return all(
                self._state(session_id, identity).status == "consumed" for identity in input_ids
            )

    def _pending_state(self, session: SessionRecord, input_id: str) -> InputState:
        state = self._state(session.session_id, input_id)
        if state.status != "pending":
            raise PersistenceError(f"Session queue references a terminal input: {input_id}")
        return state

    def _pending_item(self, session: SessionRecord, state: InputState) -> QueuedInput | None:
        if state.history_epoch != session.history_epoch and (
            state.history_bound or state.source in {"plugin", "delegation"}
        ):
            return None
        record = self.store.load_input(session.session_id, state.input_id)
        if record is None:
            raise PersistenceError(f"accepted input body not found: {state.input_id}")
        if not state.matches_intent(record):
            raise PersistenceError("input lifecycle routing does not match its accepted intent")
        item = record.item
        if item.history_epoch != session.history_epoch:
            item = item.model_copy(
                update={
                    "history_epoch": session.history_epoch,
                    "prompt_policy_checked": False,
                    "prospective_run_id": None,
                    "extension_catalog_id": None,
                    "extension_generations": (),
                }
            )
        return item

    def accept_host(
        self,
        session_id: str,
        request: HostSessionInput,
        *,
        select_driver: Callable[[str | None], RunDriverBinding | None] | None = None,
    ) -> tuple[HostInputReceipt, QueuedInput | None, SessionRecord, tuple[EventRecord, ...]]:
        queue_id = request.queue_id or new_id("queued")
        with self.store.session_read(session_id):
            session = self.store.load_session_record(session_id)
            if request.history_epoch is not None and request.history_epoch != session.history_epoch:
                raise SessionCommitConflict("Host input belongs to withdrawn Session history")
            record = self.store.load_input(session_id, queue_id)
            if record is not None:
                return self._host_receipt(session, request, record), None, session, ()
        # Driver selection may invoke Host policy; it must not own a storage lock.
        if request.driver_id is not None and select_driver is None:
            raise ValueError("Host input Driver selection is unavailable")
        driver = select_driver(request.driver_id) if select_driver is not None else None
        with self.store.session_mutation(session_id):
            session = self.store.load_session_record(session_id)
            if request.history_epoch is not None and request.history_epoch != session.history_epoch:
                raise SessionCommitConflict("Host input belongs to withdrawn Session history")
            record = self.store.load_input(session_id, queue_id)
            if record is not None:
                return self._host_receipt(session, request, record), None, session, ()
            session, item, _, events = self.enqueue(
                snapshot=RuntimeSessionSnapshot(session=session),
                prompt=request.prompt,
                driver=driver,
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
            events,
        )

    def _host_receipt(
        self, session: SessionRecord, request: HostSessionInput, record: InputRecord
    ) -> HostInputReceipt:
        existing = record.item
        validate_host_input(record, request)
        state = self._state(session.session_id, existing.queue_id)
        if state.status in {"cancelled", "withdrawn"}:
            raise ValueError("Host input was already accepted and has been withdrawn or cancelled")
        message = (
            self.store.load_message(session.session_id, state.message_ids[-1])
            if state.message_ids
            else None
        )
        return HostInputReceipt(
            session_id=session.session_id,
            queue_id=existing.queue_id,
            accepted_at=existing.queued_at,
            message_id=message.message_id if message else None,
            run_id=message.run_id if message else None,
        )

    def enqueue(
        self,
        *,
        snapshot: RuntimeSessionSnapshot,
        prompt: str,
        driver: RunDriverBinding | None = None,
        skill_activations: tuple[SkillActivationContent, ...] = (),
        mode: QueuedInputMode = "follow_up",
        title: str | None,
        resource_refs: tuple[str, ...] = (),
        attachments: tuple[InputFile, ...] = (),
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
    ) -> tuple[SessionRecord, QueuedInput, int, tuple[EventRecord, ...]]:
        batch_id = new_id("batch") if coalesce_key is not None else None
        item = QueuedInput(
            history_epoch=snapshot.session.history_epoch,
            history_bound=history_bound,
            queue_id=queue_id or new_id("queued"),
            prompt=prompt,
            skill_activations=skill_activations,
            driver=driver,
            mode=mode,
            title=title,
            resource_refs=resource_refs,
            attachments=attachments,
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
            record = self.store.load_input(latest.session_id, item.queue_id)
            if record is not None:
                if not same_queued_input(record.item, item):
                    raise ValueError("Input id is already bound to another input")
                state = self._state(latest.session_id, item.queue_id)
                if state.status in {"cancelled", "withdrawn"}:
                    raise ValueError(
                        "Input was already accepted and has been withdrawn or cancelled"
                    )
                return latest, record.item, len(latest.pending_input_ids), ()
            staged = self.stage_batch(latest, (item,))
            committed = self.store.commit_session(
                SessionCommit(
                    before=latest,
                    inputs=staged.inputs,
                    input_changes=staged.changes,
                    events=(
                        queued_input_event(
                            latest, item, len(latest.pending_input_ids) + 1 - len(staged.superseded)
                        ),
                    ),
                )
            )
        return committed.session, item, len(committed.session.pending_input_ids), committed.events

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
        driver: RunDriverBinding | None = None,
        coalesce_key: str | None = None,
        caused_by_run_id: str | None = None,
        session_control_controller_id: str | None = None,
        session_control_activation_id: str | None = None,
    ) -> tuple[
        SessionRecord,
        tuple[QueuedInput, ...],
        int,
        tuple[QueuedInput, ...],
        tuple[EventRecord, ...],
    ]:
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
                driver=driver,
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
            staged = self.stage_batch(latest, queued_items)
            committed = self.store.commit_session(
                SessionCommit(
                    before=latest,
                    inputs=staged.inputs,
                    input_changes=staged.changes,
                    events=(
                        EventRecord(
                            session_id=latest.session_id,
                            run_id=latest.active_run_id,
                            event_type="session_input_batch_queued",
                            payload={
                                "batch_id": batch_id,
                                "queue_ids": [item.queue_id for item in queued_items],
                                "batch_size": len(queued_items),
                                "mode": queued_items[0].mode,
                                "queue_size": len(latest.pending_input_ids)
                                + len(queued_items)
                                - len(staged.superseded),
                                "plugin_id": plugin_id,
                                "contribution_id": contribution_id,
                                "coalesce_key": coalesce_key,
                                "superseded_queue_ids": [
                                    item.queue_id for item in staged.superseded
                                ],
                            },
                        ),
                    ),
                )
            )
        return (
            committed.session,
            queued_items,
            len(committed.session.pending_input_ids),
            staged.superseded,
            committed.events,
        )

    def stage_batch(
        self,
        session: SessionRecord,
        queued_items: tuple[QueuedInput, ...],
        *,
        staged_inputs: tuple[InputRecord, ...] = (),
        cancelled_ids: frozenset[str] = frozenset(),
    ) -> InputQueueChange:
        if not queued_items:
            raise ValueError("queued input batch cannot be empty")
        if any(item.history_epoch != session.history_epoch for item in queued_items):
            raise SessionCommitConflict("Queued input belongs to withdrawn Session history")
        first = queued_items[0]
        if any(item.driver != first.driver for item in queued_items):
            raise ValueError("an atomic input batch must select one Driver contract")
        superseded = []
        if first.coalesce_key is not None:
            prepared = {record.item.queue_id: record for record in staged_inputs}
            for input_id in (*session.pending_input_ids, *prepared):
                if input_id in cancelled_ids:
                    continue
                state = (
                    InputState.pending(prepared[input_id])
                    if input_id in prepared
                    else self._pending_state(session, input_id)
                )
                if (
                    state.source == first.source
                    and state.plugin_id == first.plugin_id
                    and state.contribution_id == first.contribution_id
                    and state.coalesce_key == first.coalesce_key
                ):
                    item = (
                        prepared[input_id].item
                        if input_id in prepared
                        else self._pending_item(session, state)
                    )
                    if item is not None:
                        superseded.append(item)
        return InputQueueChange(
            inputs=tuple(
                InputRecord(session_id=session.session_id, item=item) for item in queued_items
            ),
            changes=tuple(
                InputStateChange(input_id=item.queue_id, status="cancelled") for item in superseded
            ),
            superseded=tuple(superseded),
        )

    def eligible(
        self,
        snapshot: RuntimeSessionSnapshot,
        *,
        mode: QueuedInputMode | None = None,
        limit: int | None = None,
    ) -> list[QueuedInput]:
        return self.eligible_session(snapshot.session, mode=mode, limit=limit)

    def bound_drivers(self, session: SessionRecord) -> tuple[RunDriverBinding, ...]:
        return tuple(
            item.driver for item in self.eligible_session(session) if item.driver is not None
        )

    def eligible_session(
        self,
        session: SessionRecord,
        *,
        mode: QueuedInputMode | None = None,
        limit: int | None = None,
    ) -> list[QueuedInput]:
        valid = []
        with self.store.session_read(session.session_id):
            session = self.store.load_session_record(session.session_id)
            for input_id in session.pending_input_ids:
                state = self._pending_state(session, input_id)
                if mode is not None and state.mode != mode:
                    continue
                item = self._pending_item(session, state)
                if item is not None:
                    valid.append(item)
                    if limit is not None and len(valid) >= limit:
                        break
        return valid

    def eligible_units(
        self,
        snapshot: RuntimeSessionSnapshot,
        *,
        mode: QueuedInputMode | None = None,
    ) -> list[tuple[QueuedInput, ...]]:
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
                or any(member.driver != item.driver for member in candidate)
                or [member.batch_index for member in candidate] != list(range(item.batch_size))
            ):
                raise PersistenceError("accepted input batch is incomplete")
            units.append(candidate)
            index += item.batch_size
        return units

    def peek(self, snapshot: RuntimeSessionSnapshot) -> QueuedInput | None:
        items = self.eligible(snapshot, limit=1)
        return items[0] if items else None

    def find_session(self, session: SessionRecord, queue_id: str) -> QueuedInput | None:
        with self.store.session_read(session.session_id):
            session = self.store.load_session_record(session.session_id)
            if queue_id not in session.pending_input_ids:
                return None
            return self._pending_item(session, self._pending_state(session, queue_id))

    def cancel_user_input(
        self, *, session_id: str, queue_id: str
    ) -> tuple[SessionRecord, QueuedInput, tuple[EventRecord, ...]] | None:
        with self.store.session_mutation(session_id):
            latest = self.store.load_session_record(session_id)
            item = self.find_session(latest, queue_id)
            if item is None or item.source != "user":
                return None
            committed = self.store.commit_session(
                SessionCommit(
                    before=latest,
                    input_changes=(InputStateChange(input_id=queue_id, status="cancelled"),),
                    events=(
                        EventRecord(
                            session_id=session_id,
                            run_id=latest.active_run_id,
                            event_type="queued_input_cancelled",
                            payload={
                                "queue_id": item.queue_id,
                                "mode": item.mode,
                                "source": item.source,
                            },
                        ),
                    ),
                )
            )
            return committed.session, item, committed.events

    def after_withdrawal(self, session: SessionRecord) -> tuple[SessionRecord, tuple[str, ...]]:
        cancelled = tuple(
            identity
            for identity in session.pending_input_ids
            if (state := self._pending_state(session, identity)).history_bound
            or state.source in {"plugin", "delegation"}
        )
        return session, cancelled

    def without_session_control(
        self,
        session: SessionRecord,
        *,
        controller_id: str,
        activation_id: str,
    ) -> tuple[SessionRecord, tuple[str, ...]]:
        cancelled = tuple(
            identity
            for identity in session.pending_input_ids
            if (state := self._pending_state(session, identity)).session_control_controller_id
            == controller_id
            and state.session_control_activation_id == activation_id
        )
        return session, cancelled


def queued_input_event(session: SessionRecord, item: QueuedInput, queue_size: int) -> EventRecord:
    """Notify acceptance using routing metadata; the Input keeps its body."""
    return EventRecord(
        session_id=session.session_id,
        run_id=session.active_run_id,
        event_type="follow_up_input_queued"
        if item.mode == "follow_up"
        else "steering_input_queued",
        payload={
            "queue_id": item.queue_id,
            "mode": item.mode,
            "queue_size": queue_size,
            "resource_refs": list(item.resource_refs),
            "source": item.source,
            "role": item.role,
            "scaffold": item.scaffold,
            "semantic_tag": item.semantic_tag,
            "host_source_id": item.host_source_id,
            "delegation_id": item.delegation_id,
            "plugin_id": item.plugin_id,
            "contribution_id": item.contribution_id,
            "skill_names": [skill.name for skill in item.skill_activations],
            "skill_content_digests": [skill.content_digest for skill in item.skill_activations],
        },
    )


def input_message_events(
    run: RunRecord,
    inputs: Sequence[QueuedInput],
    messages: Sequence[MessageRecord],
    *,
    queued: bool,
    turn_id: str | None = None,
) -> tuple[EventRecord, ...]:
    """Reference the last message of each accepted input's indivisible frame."""
    by_input = {message.input_id: message for message in messages}
    events = []
    for item in inputs:
        message = by_input[item.queue_id]
        kind = (
            "queued_input_consumed"
            if turn_id is not None
            else {"host": "host_message_added", "plugin": "plugin_message_added"}.get(
                item.source, "user_message_added"
            )
        )
        payload: dict[str, object] = {
            "message_id": message.message_id,
            "source": item.source,
            "host_source_id": item.host_source_id,
            "plugin_id": item.plugin_id,
            "contribution_id": item.contribution_id,
        }
        if turn_id is not None:
            payload.update(queue_id=item.queue_id, mode=item.mode, turn_id=turn_id)
        else:
            payload.update(
                queued_input_id=item.queue_id if queued else None,
                role=message.role,
                scaffold=message.scaffold,
                semantic_tag=message.semantic_tag,
                resource_refs=list(item.resource_refs),
                skill_names=[skill.name for skill in item.skill_activations],
                skill_content_digests=[skill.content_digest for skill in item.skill_activations],
            )
        events.append(
            EventRecord(
                session_id=run.session_id,
                run_id=run.run_id,
                turn_id=turn_id,
                event_type=kind,
                payload=payload,
            )
        )
    return tuple(events)


def project_pending_user_inputs(
    session: SessionRecord,
    inputs: list[QueuedInput],
) -> tuple[QueuedUserInputView, ...]:
    return tuple(
        project_queued_user_input(item, waiting_on_run_id=session.active_run_id)
        for item in inputs
        if item.source == "user"
    )


def consumption_changes(
    input_ids: tuple[str, ...],
    messages: list[MessageRecord] | tuple[MessageRecord, ...],
) -> tuple[InputStateChange, ...]:
    return tuple(
        InputStateChange(
            input_id=identity,
            status="consumed",
            message_ids=tuple(
                message.message_id for message in messages if message.input_id == identity
            ),
        )
        for identity in input_ids
    )


def project_queued_user_input(
    item: QueuedInput, *, waiting_on_run_id: str | None
) -> QueuedUserInputView:
    return QueuedUserInputView(
        queue_id=item.queue_id,
        prompt=item.prompt,
        mode=item.mode,
        queued_at=item.queued_at,
        waiting_on_run_id=waiting_on_run_id,
        skills=tuple(
            InputSkillReference(name=skill.name, text_offset=skill.text_offset, order=skill.order)
            for skill in item.skill_activations
        ),
        attachments=item.attachments,
        resource_refs=item.resource_refs,
    )


def same_queued_input(first: QueuedInput, second: QueuedInput) -> bool:
    """Compare the submitted input, independent of its later execution bindings."""
    fields = (
        "prompt",
        "mode",
        "history_bound",
        "title",
        "resource_refs",
        "attachments",
        "source",
        "role",
        "scaffold",
        "semantic_tag",
        "host_source_id",
        "delegation_id",
        "plugin_id",
        "contribution_id",
    )
    return (
        all(getattr(first, name) == getattr(second, name) for name in fields)
        and (first.driver.driver_id if first.driver else None)
        == (second.driver.driver_id if second.driver else None)
        and [(item.name, item.text_offset, item.order) for item in first.skill_activations]
        == [(item.name, item.text_offset, item.order) for item in second.skill_activations]
    )


def validate_host_input(record: InputRecord, request: HostSessionInput) -> None:
    """Keep a fixed Host input identity bound to its originally accepted request."""
    existing = record.item
    if (
        existing.host_source_id != request.host_source_id
        or existing.source != "host"
        or existing.prompt != request.prompt
        or existing.title != request.title
        or existing.mode != request.mode
        or existing.resource_refs != request.resource_refs
        or existing.role != request.role
        or existing.scaffold != request.scaffold
        or existing.semantic_tag != request.semantic_tag
        or existing.history_bound != (request.history_epoch is not None)
        or (existing.driver.driver_id if existing.driver else None) != request.driver_id
    ):
        raise ValueError("Host input queue id is already bound to another input")
