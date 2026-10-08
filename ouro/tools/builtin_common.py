"""Shared parameter contracts and result construction for built-in tools."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict

from ouro.core.base import utc_now
from ouro.core.harness_calls import HarnessStateChange
from ouro.tools.base import ToolInvocation, ToolResult


class BuiltinArguments(BaseModel):
    """The parameter contract used for both tool schemas and handler inputs."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def _json_integer(value: object) -> object:
    # JSON Schema integers include numbers with a zero fractional part. Python
    # handlers need int values for indexing and iteration; booleans stay invalid.
    return int(value) if isinstance(value, float) and value.is_integer() else value


JsonInteger = Annotated[int, BeforeValidator(_json_integer)]


def _tool_result(
    invocation: ToolInvocation,
    *,
    content: str,
    structured: dict[str, object] | None = None,
    is_error: bool = False,
    harness_change: HarnessStateChange | None = None,
) -> ToolResult:
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        content=content,
        structured=structured or {},
        is_error=is_error,
        harness_change=harness_change,
        ended_at=utc_now(),
    )
