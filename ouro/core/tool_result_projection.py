"""Pure, request-local expansion of tool results without rewriting execution facts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ouro.core.content import ContentPart
from ouro.core.messages import ModelMessage, model_visible_message


@dataclass(frozen=True)
class ToolResultPresentation:
    """Content answering the call, followed by zero or more contextual messages.

    The projector owns representation, not call identity or message authority.
    Context is attributed to its call and delivered only after its result batch.
    """

    content: tuple[ContentPart, ...]
    context: tuple[tuple[ContentPart, ...], ...] = ()


ToolResultProjector = Callable[[ModelMessage], ToolResultPresentation]


def project_tool_result_messages(
    messages: Sequence[ModelMessage],
    project_result: ToolResultProjector,
) -> list[ModelMessage]:
    """Expand results in one pass, keeping consecutive result batches uninterrupted.

    Context preparation supplies complete tool exchanges. This boundary preserves
    their order and references; it neither executes tools nor edits stored history.
    Calling it again on canonical input reconstructs the same presentation.
    """

    projected: list[ModelMessage] = []
    context: list[ModelMessage] = []
    for original in messages:
        message = model_visible_message(original)
        if message.role != "tool_result":
            projected.extend(context)
            context.clear()
            projected.append(message)
            continue
        reference = message.tool_result
        if reference is None:
            raise ValueError("tool_result message is missing its tool result reference")
        presentation = project_result(message)
        attributes = {"tool_name": reference.tool_name, "tool_call_id": reference.tool_call_id}
        if reference.producer.kind == "driver":
            projected.extend(context)
            context.clear()
            projected.append(
                model_visible_message(
                    ModelMessage(
                        role="user",
                        semantic_tag="driver-tool-result",
                        semantic_attributes={
                            **attributes,
                            "is_error": "true" if reference.is_error else "false",
                        },
                        content=[
                            *presentation.content,
                            *(part for fragment in presentation.context for part in fragment),
                        ],
                    )
                )
            )
            continue
        projected.append(message.model_copy(update={"content": list(presentation.content)}))
        for fragment in presentation.context:
            context.append(
                model_visible_message(
                    ModelMessage(
                        role="user",
                        semantic_tag="tool-context",
                        semantic_attributes=attributes,
                        content=list(fragment),
                    )
                )
            )
    projected.extend(context)
    return projected
