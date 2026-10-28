from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field, JsonValue

from ouro.application.contracts.base import ApplicationDTO
from ouro.core.tool_batches import ToolCallStatus

RuntimeToolCallStatus = ToolCallStatus | Literal["streaming", "cancelling"]
RUNTIME_TOOL_ARGUMENT_TAIL_MAX_CHARS = 256
InteractionKind = Literal["approval", "question"]


@dataclass(frozen=True)
class PendingInteraction:
    kind: InteractionKind
    run_id: str
    tool_call_id: str
    tool_name: str
    request_id: str | None = None
    request: object | None = None
    canonical_tool_id: str | None = None
    plugin_id: str | None = None
    plugin_class: Literal["builtin", "user"] | None = None
    session_id: str | None = None
    delegation_id: str | None = None
    agent_name: str | None = None

    @property
    def is_subagent(self) -> bool:
        return self.delegation_id is not None


class RuntimeToolCallActivity(ApplicationDTO):
    """Host-neutral projection of a tool call's current runtime activity.

    While ``status`` is ``streaming``, ``tool_call_id`` is a stream-scoped
    projection identity and ``tool_name`` contains the received name fragments.
    ``status`` owns the lifecycle; ``argument_stream_tail`` is bounded preview
    content retained only while the call is ``streaming``. Canonical calls use
    ``arguments`` instead.
    """

    batch_id: str | None = None
    tool_call_id: str
    tool_name: str
    status: RuntimeToolCallStatus
    source_index: int = Field(ge=0)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    argument_stream_tail: str | None = Field(
        default=None,
        max_length=RUNTIME_TOOL_ARGUMENT_TAIL_MAX_CHARS,
        description=(
            "Bounded verbatim suffix of streaming arguments; "
            "None before argument streaming and outside the streaming state"
        ),
    )
    result_data: dict[str, JsonValue] = Field(default_factory=dict)
    requested_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
