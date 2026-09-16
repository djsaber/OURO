"""Shared argument validation and result construction for built-in tools."""

from __future__ import annotations

from collections.abc import Mapping

from ouro.core.base import utc_now
from ouro.core.harness_calls import HarnessStateChange
from ouro.tools.base import ToolInvocation, ToolResult


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


def _string_arg(arguments: Mapping[str, object], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"tool argument '{name}' must be a non-empty string")
    return value


def _boolean_arg(arguments: Mapping[str, object], name: str, *, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"tool argument '{name}' must be a boolean")
    return value
