from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import Field

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.messages import MessageRecord
from ouro.core.tools import ToolDefinition
from ouro.prompts import render_agent_skills_prompt


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
        available_skills: Sequence[Any] | None = None,
    ) -> None:
        self.max_messages = max_messages
        self.available_skills = list(available_skills or [])

    def build(
        self,
        *,
        messages: list[MessageRecord],
        tools: list[ToolDefinition],
        system_prompt: str = "",
        added_context: list[str] | None = None,
    ) -> ContextView:
        selected = messages[-self.max_messages :]
        skills_prompt = render_agent_skills_prompt(self.available_skills)
        context_fragments = [
            *([skills_prompt] if skills_prompt else []),
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
            context_metadata={"truncated": len(messages) > len(selected)},
        )
