from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Self

from pydantic import Field, model_validator

from ouro.application.contracts.base import ApplicationDTO
from ouro.application.contracts.execution import HostInputReceipt
from ouro.application.inputs.skills import SkillInvocationRequest
from ouro.core.approvals import SessionApprovalMode
from ouro.core.base import new_id
from ouro.core.input_content import InputContent, InputFile
from ouro.core.interactions import SessionInteractionMode
from ouro.core.reasoning import ReasoningRequest
from ouro.core.results import RunResult
from ouro.core.session_controls import SessionInputMode


class UserInputRequest(ApplicationDTO):
    """One user-authored input, including stable retry and destination identities."""

    prompt: str
    session_id: str | None = None
    new_session_id: str | None = None
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

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        if self.session_id is not None and any(
            value is not None
            for value in (
                self.new_session_id,
                self.initial_model,
                self.initial_reasoning_request,
                self.workspace,
            )
        ):
            raise ValueError("Initial Session settings require a new Session")
        if not (
            self.prompt.strip() or self.skill_invocations or self.resource_refs or self.attachments
        ):
            raise ValueError("run input requires a prompt or skill invocation")
        if self.skill_invocations:
            if self.queue_mode != "follow_up":
                raise ValueError("skill invocation cannot steer an active run")
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
    def input_resource_refs(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                self.resource_refs
                or InputContent(text=self.prompt, attachments=self.attachments).resource_refs
            )
        )


@dataclass(frozen=True)
class UserInputSubmission:
    """Durable acceptance and an optional wait that owns no execution resources."""

    receipt: HostInputReceipt
    _result: Callable[[], Awaitable[RunResult]]

    async def wait(self) -> RunResult:
        return await self._result()
