from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from math import ceil
from typing import Any

from pydantic import ConfigDict, Field, model_validator

from ouro.core.base import Metadata, MetadataField, OuroModel
from ouro.core.content import MEDIA_CONTENT_COST, MediaContent
from ouro.core.context_budget import ContextBudget
from ouro.core.context_providers import ContextFragment
from ouro.core.context_selection import ContextSelection
from ouro.core.message_semantics import render_semantic_envelope
from ouro.core.messages import (
    MessageRecord,
    ModelMessage,
    TextContent,
    model_visible_message,
)
from ouro.core.request_manifest import (
    RequestContentReference,
    RequestManifest,
    RequestMessage,
    RequestMessageReference,
)
from ouro.core.tools import ToolDefinition

TokenEstimator = Callable[[str], int]


class ContextMetrics(OuroModel):
    """Measured input and request limits; budget indicators are derived facts."""

    model_config = ConfigDict(extra="forbid")

    estimated_input_tokens: int = Field(ge=0)
    estimated_fixed_tokens: int = Field(ge=0)
    budget: ContextBudget | None = None
    estimator_id: str = "builtin.conservative"
    estimator_version: str = "2"

    @model_validator(mode="after")
    def _validate_measurement(self) -> ContextMetrics:
        if self.estimated_fixed_tokens > self.estimated_input_tokens:
            raise ValueError("fixed tokens must not exceed the total input estimate")
        if not self.estimator_id.strip() or not self.estimator_version.strip():
            raise ValueError("context estimator identity must not be empty")
        return self

    @property
    def context_window_tokens(self) -> int | None:
        return self.budget.context_window if self.budget is not None else None

    @property
    def reserved_output_tokens(self) -> int:
        return self.budget.reserved_output_tokens if self.budget is not None else 0

    @property
    def input_budget_tokens(self) -> int | None:
        return self.budget.input_tokens if self.budget is not None else None

    @property
    def estimated_remaining_tokens(self) -> int | None:
        return (
            self.budget.input_tokens - self.estimated_input_tokens
            if self.budget is not None
            else None
        )

    @property
    def estimated_utilization_ratio(self) -> float | None:
        return (
            self.estimated_input_tokens / self.budget.input_tokens
            if self.budget is not None
            else None
        )

    @property
    def over_budget(self) -> bool:
        return self.budget is not None and self.estimated_input_tokens > self.budget.input_tokens

    def with_budget(self, budget: ContextBudget) -> ContextMetrics:
        """Apply another request's capacity without changing the input measurement."""
        return self.model_copy(update={"budget": budget})


class ProviderContext(OuroModel):
    """Provider-neutral arguments accepted by OURO's unified Provider boundary."""

    system_prompt: str = ""
    messages: list[ModelMessage] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)


class ContextView(OuroModel):
    """One prepared Provider value and the estimates made from that value."""

    model_config = ConfigDict(extra="forbid")

    selection: ContextSelection = Field(default_factory=ContextSelection)
    context_id: str | None = None
    provider_context: ProviderContext = Field(default_factory=ProviderContext)
    manifest: RequestManifest | None = None
    metrics: ContextMetrics = Field(
        default_factory=lambda: ContextMetrics(
            estimated_input_tokens=0,
            estimated_fixed_tokens=0,
        )
    )
    context_metadata: Metadata = MetadataField
    estimated_summary_tokens: int = Field(default=0, ge=0)


class ContextBuilder:
    def __init__(
        self,
        *,
        skill_instructions: str = "",
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self.skill_instructions = skill_instructions.strip()
        self.token_estimator = token_estimator or estimate_text_tokens
        # Only small immutable reference keys and costs for the last active input.
        # Dynamic/literal input and custom estimators are evaluated on every build.
        self._message_estimates: dict[str, int] = {}

    def build(
        self,
        *,
        messages: Sequence[MessageRecord | ModelMessage],
        tools: list[ToolDefinition],
        system_prompt: str = "",
        added_context: Sequence[str | ContextFragment] | None = None,
        additional_skill_instructions: str = "",
        budget: ContextBudget | None = None,
        source_messages: Sequence[MessageRecord] = (),
    ) -> ContextView:
        """Prepare a request from registered tool definitions and canonical context."""

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
            (
                render_semantic_envelope(fragment.text, fragment.semantic_tag)
                if fragment.semantic_tag is not None
                else fragment.text
            )
            for fragment in context_fragments
            if fragment.target == "system_prompt"
        ]
        composed_prompt = compose_system_prompt(
            system_prompt,
            skill_instructions,
            *prompt_fragments,
        )
        context_messages = [
            ModelMessage.text(
                role=fragment.role,
                text=fragment.text,
                semantic_tag=fragment.semantic_tag,
            )
            for fragment in context_fragments
            if fragment.target == "message"
        ]
        model_messages = [
            ModelMessage.from_record(message) if isinstance(message, MessageRecord) else message
            for message in messages
        ]
        model_tools = [_project_tool_definition(tool).model_dump(mode="json") for tool in tools]
        originals = [*context_messages, *model_messages]
        prepared_messages = [model_visible_message(message) for message in originals]
        sources = {message.message_id: message for message in source_messages}
        manifest_sources = [None] * len(context_messages) + [
            sources.get(message.message_id) if isinstance(message, MessageRecord) else None
            for message in messages
        ]
        manifest_messages = [
            RequestMessage.prepared(message, source)
            for message, source in zip(prepared_messages, manifest_sources, strict=True)
        ]
        previous = self._message_estimates if self.token_estimator is estimate_text_tokens else {}
        estimates: dict[str, int] = {}
        message_tokens = []
        for message, manifest in zip(prepared_messages, manifest_messages, strict=True):
            key = (
                _message_estimate_key(manifest, sources)
                if self.token_estimator is estimate_text_tokens
                else None
            )
            cost = previous.get(key) if key is not None else None
            if cost is None:
                cost = _estimate_visible_message_tokens(message, self.token_estimator)
            message_tokens.append(cost)
            if key is not None:
                estimates[key] = cost
        self._message_estimates = estimates
        fixed_tokens = self.token_estimator(composed_prompt)
        fixed_tokens += sum(
            _estimate_tool_tokens(tool, self.token_estimator) for tool in model_tools
        )
        fixed_tokens += sum(message_tokens[: len(context_messages)])
        estimated_tokens = fixed_tokens + sum(message_tokens[len(context_messages) :])
        return ContextView(
            provider_context=ProviderContext(
                messages=prepared_messages,
                system_prompt=composed_prompt,
                tools=model_tools,
            ),
            manifest=RequestManifest(
                system_prompt=composed_prompt,
                messages=manifest_messages,
                tools=model_tools,
            ),
            metrics=ContextMetrics(
                estimated_fixed_tokens=fixed_tokens,
                estimated_input_tokens=estimated_tokens,
                budget=budget,
            ),
            context_metadata={
                "skills_injected": bool(skill_instructions),
                "system_prompt_fragment_count": len(prompt_fragments),
            },
            estimated_summary_tokens=sum(
                cost
                for message, cost in zip(originals, message_tokens, strict=True)
                if message.semantic_tag == "conversation-summary"
            ),
        )


def _message_estimate_key(message: RequestMessage, sources: dict[str, MessageRecord]) -> str | None:
    """Key default estimates by immutable units, never content hashes or body copies."""
    if any(
        not isinstance(block, RequestContentReference | MediaContent) for block in message.content
    ):
        return None
    if (isinstance(message.tool_calls, list) and message.tool_calls) or (
        message.provider_continuation is not None
        and not isinstance(message.provider_continuation, RequestMessageReference)
    ):
        return None

    def reference(item: object) -> tuple[str, str] | None:
        if not isinstance(item, RequestMessageReference):
            return None
        return sources[item.message_id].session_id, item.message_id

    # Media payload/size do not contribute to the default estimator's fixed
    # media cost. Text replacements and semantic envelopes stay literal above.
    values = message.model_dump(exclude={"content", "tool_calls", "provider_continuation"})
    values["content"] = [
        (reference(block), block.block_index)
        if isinstance(block, RequestContentReference)
        else "media"
        for block in message.content
    ]
    values["tool_calls"] = reference(message.tool_calls)
    values["provider_continuation"] = reference(message.provider_continuation)
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _project_tool_definition(tool: ToolDefinition) -> ToolDefinition:
    if tool.batch_behavior != "exclusive":
        return tool
    constraint = (
        "This must be the only tool call in your response. If you include other tool calls, "
        "none of them will execute."
    )
    description = f"{tool.description}\n\n{constraint}" if tool.description else constraint
    return tool.model_copy(update={"description": description})


def _compose_skill_instructions(base: str, additional: str) -> str:
    values = tuple(value.strip() for value in (base, additional) if value.strip())
    return "\n\n".join(values)


def compose_system_prompt(system_prompt: str, *fragments: str) -> str:
    """Join instruction bodies with blank-line boundaries, preserving their content."""

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

    ascii_characters, non_ascii_characters = text_character_counts(text)
    return estimate_text_tokens_from_character_counts(
        ascii_characters=ascii_characters,
        non_ascii_characters=non_ascii_characters,
    )


def text_character_counts(text: str) -> tuple[int, int]:
    """Count estimator units in native code, without allocating for ASCII input."""

    ascii_characters = len(text) if text.isascii() else len(text.encode("ascii", errors="ignore"))
    return ascii_characters, len(text) - ascii_characters


def estimate_text_tokens_from_character_counts(
    *,
    ascii_characters: int,
    non_ascii_characters: int,
) -> int:
    """Evaluate the default estimator from an incrementally accumulated measure."""

    if ascii_characters < 0 or non_ascii_characters < 0:
        raise ValueError("character counts must be non-negative")
    if ascii_characters == 0 and non_ascii_characters == 0:
        return 0
    return max(1, ceil(ascii_characters / 4) + non_ascii_characters)


def estimate_message_tokens(
    message: MessageRecord | ModelMessage,
    estimator: TokenEstimator | None = None,
) -> int:
    active_estimator = estimator or estimate_text_tokens
    model_message = model_visible_message(message)
    return _estimate_visible_message_tokens(model_message, active_estimator)


def _estimate_visible_message_tokens(
    model_message: ModelMessage, active_estimator: TokenEstimator
) -> int:
    content_tokens = sum(
        active_estimator(block.text)
        if isinstance(block, TextContent)
        else MEDIA_CONTENT_COST
        if isinstance(block, MediaContent)
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


def _estimate_tool_tokens(tool: dict[str, Any], estimator: TokenEstimator) -> int:
    return estimator(
        json.dumps(
            {key: value for key, value in tool.items() if value is not None},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


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
            role="system",
            text=(
                "The previous assistant response was interrupted. The text below is "
                "unfinished and does not confirm that any planned action succeeded. "
                "Check the latest tool results and continue from existing progress:\n\n"
                f"{message.text_content}"
            ),
            sequence=message.sequence,
            scaffold=True,
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
