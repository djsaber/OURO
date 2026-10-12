"""Pure, request-local expansion of tool results without rewriting execution facts."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ouro.core.content import ContentPart, TextContent
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
        if reference.producer.kind == "driver":
            projected.extend(context)
            context.clear()
            projected.append(
                model_visible_message(
                    ModelMessage(
                        role="user",
                        semantic_tag="driver-tool-result",
                        content=[
                            TextContent(
                                text=(
                                    f"Tool {json.dumps(reference.tool_name)} executed by Driver "
                                    f"{json.dumps(reference.producer.driver_id)} "
                                    f"(call {json.dumps(reference.tool_call_id)}, "
                                    f"error={reference.is_error}):\n"
                                )
                            ),
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
                        content=[
                            TextContent(
                                text=(
                                    "Supplemental output from tool "
                                    f"{json.dumps(reference.tool_name)} "
                                    f"(call {json.dumps(reference.tool_call_id)}):\n"
                                )
                            ),
                            *fragment,
                        ],
                    )
                )
            )
    projected.extend(context)
    return projected
