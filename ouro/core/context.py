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
        max_messages: int = 40,
        *,
        skill_instructions: str = "",
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        if max_messages < 1:
            raise ValueError("max_messages must be at least 1")
        self.max_messages = max_messages
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
            _estimate_message_tokens(message, self.token_estimator) for message in context_messages
        )
        message_budget = (
            None if input_token_budget is None else max(0, input_token_budget - fixed_tokens)
        )
        selected = _select_recent_messages(
            messages,
            self.max_messages,
            token_budget=message_budget,
            token_estimator=self.token_estimator,
        )
        estimated_tokens = fixed_tokens + sum(
            _estimate_message_tokens(message, self.token_estimator) for message in selected
        )
        return ContextView(
            model_messages=[*context_messages, *selected],
            system_prompt=composed_prompt,
            tool_definitions=tools,
            context_metadata={
                "truncated": len(messages) > len(selected),
                "omitted_message_count": len(messages) - len(selected),
                "skills_injected": bool(self.skill_instructions),
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


def _select_recent_messages(
    messages: list[MessageRecord],
    max_messages: int,
    *,
    token_budget: int | None = None,
    token_estimator: TokenEstimator | None = None,
) -> list[MessageRecord]:
    """Select recent atomic exchanges without orphaning tool results.

    The newest exchange is always retained, even when it alone exceeds the
    approximate budget. This gives the provider a useful, protocol-valid view
    and exposes the overflow through ``ContextView.context_metadata``.
    """

    if not messages:
        return []
    estimator = token_estimator or estimate_text_tokens
    groups = _message_groups(messages)
    selected_groups: list[list[MessageRecord]] = []
    selected_count = 0
    selected_tokens = 0
    for group in reversed(groups):
        group_tokens = sum(_estimate_message_tokens(message, estimator) for message in group)
        exceeds_count = selected_count > 0 and selected_count + len(group) > max_messages
        exceeds_tokens = (
            token_budget is not None
            and selected_groups
            and selected_tokens + group_tokens > token_budget
        )
        if exceeds_count or exceeds_tokens:
            break
        selected_groups.append(group)
        selected_count += len(group)
        selected_tokens += group_tokens
    return [message for group in reversed(selected_groups) for message in group]


def _message_groups(messages: list[MessageRecord]) -> list[list[MessageRecord]]:
    groups: list[list[MessageRecord]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        index += 1
        if message.role == "assistant" and message.metadata.get("tool_calls"):
            while index < len(messages) and messages[index].role == "tool_result":
                group.append(messages[index])
                index += 1
        groups.append(group)
    return groups


def estimate_text_tokens(text: str) -> int:
    """Estimate mixed ASCII/CJK token usage without provider-specific tokenizers."""

    if not text:
        return 0
    ascii_characters = sum(ord(character) < 128 for character in text)
    non_ascii_characters = len(text) - ascii_characters
    return max(1, ceil(ascii_characters / 4) + non_ascii_characters)


def _estimate_message_tokens(
    message: MessageRecord,
    estimator: TokenEstimator,
) -> int:
    payload = {
        "role": message.role,
        "content": [block.model_dump(mode="json") for block in message.content],
        "resource_refs": message.resource_refs,
        "metadata": message.metadata,
    }
    return 4 + estimator(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _estimate_tool_tokens(tool: ToolDefinition, estimator: TokenEstimator) -> int:
    return estimator(tool.model_dump_json(exclude_none=True))
