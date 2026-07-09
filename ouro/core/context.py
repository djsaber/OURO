from __future__ import annotations

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
    def __init__(self, max_messages: int = 40) -> None:
        self.max_messages = max_messages

    def build(
        self,
        *,
        messages: list[MessageRecord],
        tools: list[ToolDefinition],
        system_prompt: str = "",
        added_context: list[str] | None = None,
    ) -> ContextView:
        selected = messages[-self.max_messages :]
        model_messages = list(selected)
        for index, text in enumerate(added_context or []):
            model_messages.insert(
                0,
                MessageRecord.text(
                    session_id=messages[-1].session_id if messages else "system",
                    role="system_synthetic",
                    text=text,
                    sequence=-(index + 1),
                ),
            )
        return ContextView(
            model_messages=model_messages,
            system_prompt=system_prompt,
            tool_definitions=tools,
            context_metadata={"truncated": len(messages) > len(selected)},
        )
