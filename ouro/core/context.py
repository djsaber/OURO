from __future__ import annotations

import json
from collections.abc import Callable
from math import ceil

from pydantic import Field

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.messages import MessageRecord
from ouro.core.tools import ToolDefinition

TokenEstimator = Callable[[str], int]


class ContextView(OuroModel):
    model_messages: list[MessageRecord] = Field(default_factory=list)
    system_prompt: str = ""
    tool_definitions: list[ToolDefinition] = Field(default_factory=list)
    context_metadata: Metadata = MetadataField


class ContextBuilder:
    def __init__(
        self,
        *,
        skill_instructions: str = "",
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self.skill_instructions = skill_instructions.strip()
        self.token_estimator = token_estimator or estimate_text_tokens

    def build(
        self,
        *,
        messages: list[MessageRecord],
        tools: list[ToolDefinition],
        system_prompt: str = "",
        added_context: list[str] | None = None,
        input_token_budget: int | None = None,
    ) -> ContextView:
        if input_token_budget is not None and input_token_budget < 1:
            raise ValueError("input_token_budget must be at least 1")
        composed_prompt = _compose_system_prompt(system_prompt, self.skill_instructions)
        context_fragments = [text for text in (added_context or []) if text]
        context_messages = [
            MessageRecord.text(
                session_id=messages[-1].session_id if messages else "system",
                role="system_synthetic",
                text=text,
                sequence=-(len(context_fragments) - index),
            )
            for index, text in enumerate(context_fragments)
        ]
        fixed_tokens = self.token_estimator(composed_prompt)
        fixed_tokens += sum(_estimate_tool_tokens(tool, self.token_estimator) for tool in tools)
        fixed_tokens += sum(
            estimate_message_tokens(message, self.token_estimator) for message in context_messages
        )
        estimated_tokens = fixed_tokens + sum(
            estimate_message_tokens(message, self.token_estimator) for message in messages
        )
        return ContextView(
            model_messages=[*context_messages, *messages],
            system_prompt=composed_prompt,
            tool_definitions=tools,
            context_metadata={
                "skills_injected": bool(self.skill_instructions),
                "estimated_fixed_tokens": fixed_tokens,
                "estimated_input_tokens": estimated_tokens,
                "input_token_budget": input_token_budget,
                "over_budget": (
                    input_token_budget is not None and estimated_tokens > input_token_budget
                ),
            },
        )


def _compose_system_prompt(system_prompt: str, skill_instructions: str) -> str:
    if not skill_instructions:
        return system_prompt
    if not system_prompt:
        return skill_instructions
    if system_prompt.endswith("\n\n"):
        separator = ""
    elif system_prompt.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return f"{system_prompt}{separator}{skill_instructions}"


def estimate_text_tokens(text: str) -> int:
    """Estimate mixed ASCII/CJK token usage without provider-specific tokenizers."""

    if not text:
        return 0
    ascii_characters = sum(ord(character) < 128 for character in text)
    non_ascii_characters = len(text) - ascii_characters
    return max(1, ceil(ascii_characters / 4) + non_ascii_characters)


def estimate_message_tokens(
    message: MessageRecord,
    estimator: TokenEstimator | None = None,
) -> int:
    active_estimator = estimator or estimate_text_tokens
    payload = {
        "role": message.role,
        "content": [block.model_dump(mode="json") for block in message.content],
        "resource_refs": message.resource_refs,
        "tool_calls": [call.model_dump(mode="json") for call in message.tool_calls],
        "tool_result": message.tool_result.model_dump(mode="json")
        if message.tool_result is not None
        else None,
        "provider_continuation": message.provider_continuation.model_dump(mode="json")
        if message.provider_continuation is not None
        else None,
        "metadata": message.metadata,
    }
    return 4 + active_estimator(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _estimate_tool_tokens(tool: ToolDefinition, estimator: TokenEstimator) -> int:
    return estimator(tool.model_dump_json(exclude_none=True))


def project_recovery_messages(messages: list[MessageRecord]) -> list[MessageRecord]:
    """Represent interrupted assistant text as recovery context, not a completed answer."""

    projected: list[MessageRecord] = []
    for message in messages:
        if (
            message.role != "assistant"
            or message.tool_calls
            or not message.metadata.get("incomplete")
        ):
            projected.append(message)
            continue
        recovery = MessageRecord.text(
            session_id=message.session_id,
            run_id=message.run_id,
            turn_id=message.turn_id,
            role="system_synthetic",
            text=(
                "A previous assistant model attempt was interrupted before completion. "
                "Treat the following text as partial work, not as a completed answer:\n\n"
                f"{message.text_content}"
            ),
            sequence=message.sequence,
        ).model_copy(
            update={
                "message_id": message.message_id,
                "metadata": {
                    **message.metadata,
                    "kind": "interrupted_assistant_recovery",
                    "source_role": "assistant",
                },
            }
        )
        projected.append(recovery)
    return projected
