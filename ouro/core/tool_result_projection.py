"""Pure, request-local expansion of tool results without rewriting execution facts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ouro.core.content import ContentPart, TextContent
from ouro.core.messages import ModelMessage, model_visible_message, model_visible_messages


@dataclass(frozen=True)
class ToolResultPresentation:
    """Content answering the call, followed by zero or more contextual messages.

    The projector owns representation, not call identity or message authority.
    Context is attributed to its call and delivered only after its result batch.
    """

    content: tuple[ContentPart, ...]
    context: tuple[tuple[ContentPart, ...], ...] = ()


ToolResultProjector = Callable[[ModelMessage], ToolResultPresentation]


def project_provider_messages(
    messages: Sequence[ModelMessage],
    project_result: ToolResultProjector,
    *,
    system_prompt: str,
    supports_inline_system_messages: bool = False,
) -> tuple[str, list[ModelMessage]]:
    """Place native instructions and expand complete result batches for a protocol encoder."""

    def append_visible(target: list[ModelMessage], original: ModelMessage) -> None:
        for message in model_visible_messages(
            original, supports_inline_system_messages=supports_inline_system_messages
        ):
            if message.role == "system" and not supports_inline_system_messages:
                if projected:
                    raise ValueError("the endpoint does not support inline system messages")
                if any(not isinstance(part, TextContent) for part in message.content):
                    raise ValueError("system instructions require text content")
                if text := message.text_content:
                    instructions.append(text)
            else:
                target.append(message)

    instructions = [system_prompt] if system_prompt else []
    projected: list[ModelMessage] = []
    context: list[ModelMessage] = []
    for original in messages:
        if original.role != "tool_result":
            projected.extend(context)
            context.clear()
            append_visible(projected, original)
            continue
        message = model_visible_message(original)
        reference = message.tool_result
        if reference is None:
            raise ValueError("tool_result message is missing its tool result reference")
        presentation = project_result(message)
        attributes = {"tool_name": reference.tool_name, "tool_call_id": reference.tool_call_id}
        if reference.producer.kind == "driver":
            projected.extend(context)
            context.clear()
            append_visible(
                projected,
                ModelMessage(
                    role="harness",
                    semantic_tag="driver-tool-result",
                    semantic_attributes={
                        **attributes,
                        "is_error": "true" if reference.is_error else "false",
                    },
                    content=[
                        *presentation.content,
                        *(part for fragment in presentation.context for part in fragment),
                    ],
                ),
            )
            continue
        projected.append(message.model_copy(update={"content": list(presentation.content)}))
        for fragment in presentation.context:
            append_visible(
                context,
                ModelMessage(
                    role="harness",
                    semantic_tag="tool-context",
                    semantic_attributes=attributes,
                    content=list(fragment),
                ),
            )
    projected.extend(context)
    return "\n\n".join(instructions), projected
