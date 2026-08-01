from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from math import ceil, isclose
from typing import Any

from pydantic import Field, model_validator

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.context_providers import ContextFragment
from ouro.core.message_semantics import render_semantic_envelope
from ouro.core.messages import (
    ImageContent,
    MessageRecord,
    ModelMessage,
    TextContent,
    model_visible_message,
)
from ouro.core.tools import ToolDefinition

TokenEstimator = Callable[[str], int]


class ContextMetrics(OuroModel):
    """Typed context-budget facts for one effective model request."""

    estimated_input_tokens: int = Field(ge=0)
    estimated_fixed_tokens: int = Field(ge=0)
    context_window_tokens: int | None = Field(default=None, ge=1)
    reserved_output_tokens: int = Field(default=0, ge=0)
    input_budget_tokens: int | None = Field(default=None, ge=1)
    estimated_remaining_tokens: int | None = None
    estimated_utilization_ratio: float | None = Field(default=None, ge=0)
    over_budget: bool = False
    estimator_id: str = "builtin.conservative"
    estimator_version: str = "2"

    @model_validator(mode="after")
    def _validate_budget_facts(self) -> ContextMetrics:
        if self.estimated_fixed_tokens > self.estimated_input_tokens:
            raise ValueError("fixed tokens must not exceed the total input estimate")
        if not self.estimator_id.strip() or not self.estimator_version.strip():
            raise ValueError("context estimator identity must not be empty")
        budget = self.input_budget_tokens
        if self.context_window_tokens is not None:
            if self.reserved_output_tokens >= self.context_window_tokens:
                raise ValueError("reserved output must be lower than the context window")
            expected_budget = self.context_window_tokens - self.reserved_output_tokens
            if budget != expected_budget:
                raise ValueError("input budget must equal context window minus reserved output")
        if budget is None:
            if self.estimated_remaining_tokens is not None:
                raise ValueError("remaining tokens require an input budget")
            if self.estimated_utilization_ratio is not None:
                raise ValueError("utilization requires an input budget")
            if self.over_budget:
                raise ValueError("over-budget state requires an input budget")
            return self
        expected_remaining = budget - self.estimated_input_tokens
        if self.estimated_remaining_tokens != expected_remaining:
            raise ValueError("remaining tokens do not match the input estimate and budget")
        expected_utilization = self.estimated_input_tokens / budget
        if self.estimated_utilization_ratio is None or not isclose(
            self.estimated_utilization_ratio,
            expected_utilization,
        ):
            raise ValueError("utilization does not match the input estimate and budget")
        if self.over_budget != (self.estimated_input_tokens > budget):
            raise ValueError("over-budget state does not match the input estimate and budget")
        return self


class ProviderContext(OuroModel):
    """Provider-neutral arguments accepted by OURO's unified Provider boundary."""

    system_prompt: str = ""
    messages: list[ModelMessage] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)


class ContextView(OuroModel):
    context_id: str | None = None
    model_messages: list[ModelMessage] = Field(default_factory=list)
    system_prompt: str = ""
    tool_definitions: list[ToolDefinition] = Field(default_factory=list)
    metrics: ContextMetrics = Field(
        default_factory=lambda: ContextMetrics(
            estimated_input_tokens=0,
            estimated_fixed_tokens=0,
        )
    )
    context_metadata: Metadata = MetadataField

    def provider_context(self) -> ProviderContext:
        return ProviderContext(
            system_prompt=self.system_prompt,
            messages=[model_visible_message(message) for message in self.model_messages],
            tools=[tool.model_dump() for tool in self.tool_definitions],
        )


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
        added_context: Sequence[str | ContextFragment] | None = None,
        additional_skill_instructions: str = "",
        input_token_budget: int | None = None,
        context_window_tokens: int | None = None,
        reserved_output_tokens: int = 0,
    ) -> ContextView:
        if input_token_budget is not None and input_token_budget < 1:
            raise ValueError("input_token_budget must be at least 1")
        if context_window_tokens is not None and context_window_tokens < 1:
            raise ValueError("context_window_tokens must be at least 1")
        if reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens must not be negative")
        if context_window_tokens is not None:
            derived_budget = context_window_tokens - reserved_output_tokens
            if derived_budget < 1:
                raise ValueError("reserved_output_tokens must be lower than the context window")
            if input_token_budget is not None and input_token_budget != derived_budget:
                raise ValueError("input_token_budget does not match the supplied context limits")
            input_token_budget = derived_budget
        skill_instructions = _compose_skill_instructions(
            self.skill_instructions,
            additional_skill_instructions,
        )
        context_fragments = [
            value if isinstance(value, ContextFragment) else ContextFragment(text=value)
            for value in (added_context or [])
            if value
        ]
        prompt_fragments = [
            render_semantic_envelope(fragment.text, fragment.semantic_tag)
            for fragment in context_fragments
            if fragment.target == "system_prompt"
        ]
        composed_prompt = _compose_system_prompt(
            system_prompt,
            skill_instructions,
            *prompt_fragments,
        )
        context_messages = [
            ModelMessage.text(
                role="system_synthetic",
                text=fragment.text,
                semantic_tag=fragment.semantic_tag,
            )
            for fragment in context_fragments
            if fragment.target == "message"
        ]
        model_messages = [ModelMessage.from_record(message) for message in messages]
        fixed_tokens = self.token_estimator(composed_prompt)
        fixed_tokens += sum(_estimate_tool_tokens(tool, self.token_estimator) for tool in tools)
        fixed_tokens += sum(
            estimate_message_tokens(message, self.token_estimator) for message in context_messages
        )
        estimated_tokens = fixed_tokens + sum(
            estimate_message_tokens(message, self.token_estimator) for message in model_messages
        )
        remaining = (
            input_token_budget - estimated_tokens if input_token_budget is not None else None
        )
        utilization = (
            estimated_tokens / input_token_budget if input_token_budget is not None else None
        )
        return ContextView(
            model_messages=[*context_messages, *model_messages],
            system_prompt=composed_prompt,
            tool_definitions=tools,
            metrics=ContextMetrics(
                estimated_fixed_tokens=fixed_tokens,
                estimated_input_tokens=estimated_tokens,
                context_window_tokens=context_window_tokens,
                reserved_output_tokens=reserved_output_tokens,
                input_budget_tokens=input_token_budget,
                estimated_remaining_tokens=remaining,
                estimated_utilization_ratio=utilization,
                over_budget=(
                    input_token_budget is not None and estimated_tokens > input_token_budget
                ),
            ),
            context_metadata={
                "skills_injected": bool(skill_instructions),
                "system_prompt_fragment_count": len(prompt_fragments),
            },
        )


def _compose_skill_instructions(base: str, additional: str) -> str:
    values = tuple(value.strip() for value in (base, additional) if value.strip())
    return "\n\n".join(values)


def _compose_system_prompt(system_prompt: str, *fragments: str) -> str:
    composed = system_prompt
    for fragment in fragments:
        if not fragment:
            continue
        if not composed:
            composed = fragment
        elif composed.endswith("\n\n"):
            composed = f"{composed}{fragment}"
        elif composed.endswith("\n"):
            composed = f"{composed}\n{fragment}"
        else:
            composed = f"{composed}\n\n{fragment}"
    return composed


def estimate_text_tokens(text: str) -> int:
    """Estimate mixed ASCII/CJK token usage without provider-specific tokenizers."""

    if not text:
        return 0
    ascii_characters = sum(ord(character) < 128 for character in text)
    non_ascii_characters = len(text) - ascii_characters
    return max(1, ceil(ascii_characters / 4) + non_ascii_characters)


def estimate_message_tokens(
    message: MessageRecord | ModelMessage,
    estimator: TokenEstimator | None = None,
) -> int:
    active_estimator = estimator or estimate_text_tokens
    model_message = model_visible_message(message)
    content_tokens = sum(
        active_estimator(block.text)
        if isinstance(block, TextContent)
        else _estimate_image_tokens(block)
        if isinstance(block, ImageContent)
        else 0
        for block in model_message.content
    )
    protocol_payload = {
        "role": model_message.role,
        "tool_calls": [call.model_dump(mode="json") for call in model_message.tool_calls],
        "tool_result": model_message.tool_result.model_dump(mode="json")
        if model_message.tool_result is not None
        else None,
        "provider_continuation": model_message.provider_continuation.model_dump(mode="json")
        if model_message.provider_continuation is not None
        else None,
    }
    return (
        4
        + content_tokens
        + active_estimator(json.dumps(protocol_payload, ensure_ascii=False, sort_keys=True))
    )


def _estimate_image_tokens(_image: ImageContent) -> int:
    """Use a bounded fallback instead of treating base64 bytes as text tokens."""

    return 2048


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
            semantic_tag="interrupted-assistant-attempt",
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
