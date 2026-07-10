from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.messages import MessageRecord
from ouro.core.tools import ToolDefinition


class ContextView(OuroModel):
    model_messages: list[MessageRecord] = Field(default_factory=list)
    system_prompt: str = ""
    tool_definitions: list[ToolDefinition] = Field(default_factory=list)
    context_metadata: Metadata = MetadataField


class ContextBuilder:
    def __init__(
        self,
        max_messages: int = 40,
        *,
        base_context: Sequence[str] = (),
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be at least 1")
        self.max_messages = max_messages
        self.base_context = tuple(text for text in base_context if text)

    def build(
        self,
        *,
        messages: list[MessageRecord],
        tools: list[ToolDefinition],
        system_prompt: str = "",
        added_context: list[str] | None = None,
    ) -> ContextView:
        selected = _select_recent_messages(messages, self.max_messages)
        context_fragments = [
            *self.base_context,
            *(added_context or []),
        ]
        context_messages = [
            MessageRecord.text(
                session_id=messages[-1].session_id if messages else "system",
                role="system_synthetic",
                text=text,
                sequence=-(len(context_fragments) - index),
            )
            for index, text in enumerate(context_fragments)
        ]
        return ContextView(
            model_messages=[*context_messages, *selected],
            system_prompt=system_prompt,
            tool_definitions=tools,
            context_metadata={
                "truncated": len(messages) > len(selected),
                "omitted_message_count": len(messages) - len(selected),
            },
        )


def _select_recent_messages(
    messages: list[MessageRecord],
    max_messages: int,
) -> list[MessageRecord]:
    """Select recent messages without orphaning leading tool results.

    OpenAI-compatible histories require a tool result to follow the assistant
    message that requested it. A strict slice can start inside that exchange,
    so move the cut point back to the requesting assistant message.
    """

    start = max(0, len(messages) - max_messages)
    while start > 0 and messages[start].role == "tool_result":
        start -= 1
    return messages[start:]
