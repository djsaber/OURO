from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Self

from pydantic import Field, model_validator

from ouro.application.contracts.base import ApplicationDTO
from ouro.application.contracts.execution import HostInputReceipt
from ouro.application.inputs.skills import SkillInvocationRequest
from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import new_id, utc_now
from ouro.core.errors import SessionBusyError
from ouro.core.input_content import InputContent, InputFile
from ouro.core.inputs import QueuedInput, SourceHistory
from ouro.core.interactions import SessionInteractionMode
from ouro.core.messages import SkillActivationContent
from ouro.core.reasoning import ReasoningRequest
from ouro.core.results import RunResult
from ouro.core.run_drivers import RunDriverBinding
from ouro.core.session_controls import (
    AttributedSessionInput,
    DelegatedTaskInput,
    HostSessionInput,
    SessionInputMode,
)
from ouro.core.state import RunRecord, SessionRecord

if TYPE_CHECKING:
    from ouro.application.runtime.extensions import RuntimeCapabilities


@dataclass(frozen=True)
class InputSessionSettings:
    """Session options committed with an accepted user input."""

    approval_mode: SessionApprovalMode | None = None
    interaction_mode: SessionInteractionMode | None = None

    def apply(self, session: SessionRecord, *, executing: bool = False) -> SessionRecord:
        changes = {
            name: value
            for name, value in (
                ("approval_mode", self.approval_mode),
                ("interaction_mode", self.interaction_mode),
            )
            if value is not None and value != getattr(session, name)
        }
        if not changes:
            return session
        if executing or session.active_run_id is not None:
            name = next(iter(changes)).replace("_", " ")
            raise SessionBusyError(
                f"cannot change {name} while the session has an active run: {session.session_id}"
            )
        now = utc_now()
        return session.model_copy(update={**changes, "updated_at": now, "last_activity_at": now})


class UserMessageEdit(ApplicationDTO):
    """A selected input and the extent of the history the user agreed to replace."""

    message_id: str
    history_epoch: int = Field(ge=0)
    message_count: int = Field(ge=1)


class UserInputRequest(ApplicationDTO):
    """One user-authored input, including stable retry and destination identities."""

    prompt: str
    session_id: str | None = None
    new_session_id: str | None = None
    edit: UserMessageEdit | None = None
    input_id: str = Field(default_factory=lambda: new_id("queued"))
    title: str | None = None
    initial_model: str | None = None
    initial_reasoning_request: ReasoningRequest | None = None
    approval_mode: SessionApprovalMode | None = None
    interaction_mode: SessionInteractionMode | None = None
    workspace: str | None = None
    queue_mode: SessionInputMode = "follow_up"
    skill_invocations: tuple[SkillInvocationRequest, ...] = ()
    resource_refs: tuple[str, ...] = ()
    attachments: tuple[InputFile, ...] = Field(default=(), max_length=16)
    source_history: SourceHistory | None = None

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        if self.edit is not None and self.session_id is None:
            raise ValueError("Editing a message requires an existing session")
        if self.source_history is not None and self.session_id is None:
            raise ValueError("Source history requires an existing session")
        if self.session_id is not None:
            if self.new_session_id is not None:
                raise ValueError("new_session_id requires a new direct-user Session")
            for name in ("initial_model", "initial_reasoning_request", "workspace"):
                if getattr(self, name) is not None:
                    raise ValueError(f"{name} can only be set when creating a session")
        if not (
            self.prompt.strip() or self.skill_invocations or self.resource_refs or self.attachments
        ):
            raise ValueError("run input requires a prompt or skill invocation")
        if self.skill_invocations:
            if any(item.arguments != self.prompt for item in self.skill_invocations):
                raise ValueError("prompt must match the skill invocation arguments")
            if len({item.name for item in self.skill_invocations}) != len(self.skill_invocations):
                raise ValueError("skill invocations must have unique names")
        content = InputContent(text=self.prompt, attachments=self.attachments)
        if self.attachments and self.resource_refs and self.resource_refs != content.resource_refs:
            raise ValueError("Attachment identities must match resource references")
        if self.session_id is None and self.new_session_id is None:
            object.__setattr__(self, "new_session_id", new_id("ses"))
        return self

    @property
    def session_settings(self) -> InputSessionSettings:
        return InputSessionSettings(self.approval_mode, self.interaction_mode)

    @property
    def input_resource_refs(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                self.resource_refs
                or InputContent(text=self.prompt, attachments=self.attachments).resource_refs
            )
        )


@dataclass(frozen=True)
class IncomingRunInput:
    """One incoming request whose durable body is built with the selected Session and skills."""

    request: UserInputRequest | AttributedSessionInput | HostSessionInput | DelegatedTaskInput
    driver: RunDriverBinding | None = None

    def new_run(self, session: SessionRecord, *, history_epoch: int | None = None) -> RunRecord:
        plugin = self.request if isinstance(self.request, AttributedSessionInput) else None
        return RunRecord(
            session_id=session.session_id,
            history_epoch=session.history_epoch if history_epoch is None else history_epoch,
            status="running",
            driver=self.driver or RunDriverBinding(),
            origin="plugin" if plugin else "user",
            trigger="plugin" if plugin else "user",
            initiator_id=plugin.plugin_id if plugin else None,
        )

    @property
    def skills(self) -> tuple[SkillInvocationRequest, ...]:
        return self.request.skill_invocations if isinstance(self.request, UserInputRequest) else ()

    @property
    def settings(self) -> InputSessionSettings:
        return (
            self.request.session_settings
            if isinstance(self.request, UserInputRequest)
            else InputSessionSettings()
        )

    @property
    def attachments(self) -> tuple[InputFile, ...]:
        return self.request.attachments if isinstance(self.request, UserInputRequest) else ()

    @property
    def resource_refs(self) -> tuple[str, ...]:
        request = self.request
        if isinstance(request, UserInputRequest):
            return request.input_resource_refs
        return () if isinstance(request, AttributedSessionInput) else request.resource_refs

    def materialize(
        self,
        history_epoch: int,
        *,
        skills: tuple[SkillActivationContent, ...] = (),
        enqueue: bool = False,
        active: RunRecord | None = None,
    ) -> QueuedInput:
        request = self.request
        if isinstance(request, UserInputRequest):
            return QueuedInput(
                history_epoch=history_epoch,
                queue_id=request.input_id,
                prompt=request.prompt,
                driver=self.driver,
                title=request.title,
                mode=request.queue_mode,
                resource_refs=request.input_resource_refs,
                attachments=request.attachments,
                source_history=request.source_history,
                skill_activations=skills,
                queued_at=utc_now(),
                source="user",
                role="user",
                semantic_tag=None,
            )
        item = partial(
            QueuedInput,
            history_epoch=history_epoch,
            prompt=request.prompt,
            driver=self.driver,
            title=request.title,
            mode=request.mode,
            queued_at=utc_now(),
            role=request.role,
            semantic_tag=request.semantic_tag,
            semantic_attributes=request.semantic_attributes,
            caused_by_run_id=active.run_id if active is not None else None,
        )
        if isinstance(request, AttributedSessionInput):
            return item(
                queue_id=new_id("queued"),
                source="plugin",
                plugin_id=request.plugin_id,
                contribution_id=request.contribution_id,
                generation_id=request.generation_id,
                execution_contract_hash=request.execution_contract_hash,
            )
        if isinstance(request, HostSessionInput):
            return item(
                queue_id=(request.queue_id if enqueue else None) or new_id("queued"),
                source="host",
                host_source_id=request.host_source_id,
                resource_refs=request.resource_refs,
                history_bound=enqueue and request.history_epoch is not None,
            )
        return item(
            queue_id=new_id("queued"),
            source="delegation",
            delegation_id=request.delegation_id,
            resource_refs=request.resource_refs,
        )


@dataclass
class PreparedUserInput:
    """A prepared user input owns its admission catalog until execution claims it."""

    session: SessionRecord
    item: QueuedInput
    run: RunRecord
    snapshot: RuntimeCapabilities | None = None

    def take_snapshot(self) -> RuntimeCapabilities | None:
        snapshot, self.snapshot = self.snapshot, None
        return snapshot

    def close(self) -> None:
        snapshot = self.take_snapshot()
        if snapshot is not None:
            snapshot.close()


@dataclass(frozen=True)
class UserInputSubmission:
    """Durable acceptance and an optional wait that owns no execution resources."""

    receipt: HostInputReceipt
    _result: Callable[[], Awaitable[RunResult]]

    async def wait(self) -> RunResult:
        return await self._result()


def queued_run(
    session: SessionRecord, items: tuple[QueuedInput, ...], *, direct_first: bool
) -> RunRecord:
    first = items[0]
    producer = next((item for item in items if item.source != "user"), None)
    return RunRecord(
        run_id=first.prospective_run_id
        if first.prompt_policy_checked and first.prospective_run_id
        else new_id("run"),
        session_id=session.session_id,
        history_epoch=session.history_epoch,
        status="running",
        origin=producer.source if producer else ("user" if direct_first else "follow_up"),
        trigger=producer.source if producer else "user",
        driver=first.driver or RunDriverBinding(),
        initiator_id=_queued_input_initiator(producer),
        parent_run_id=next(
            (item.caused_by_run_id for item in items if item.caused_by_run_id is not None), None
        ),
    )


def _queued_input_initiator(item: QueuedInput | None) -> str | None:
    if item is None:
        return None
    if item.source == "host":
        return item.host_source_id
    if item.source == "plugin":
        return item.continuation_decision_id or item.plugin_id
    if item.source == "delegation" and item.delegation_id is not None:
        return f"ouro.subagents/{item.delegation_id}"
    return None
