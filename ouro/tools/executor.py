from __future__ import annotations

from collections.abc import Mapping

from ouro.core.base import utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.state import ApprovalRecord
from ouro.core.tools import PermissionMode
from ouro.tools.base import (
    ToolExecutionAborted,
    ToolExecutionContext,
    ToolExecutionOutcome,
    ToolInvocation,
    ToolResult,
)
from ouro.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        permission_overrides: Mapping[str, PermissionMode] | None = None,
    ) -> None:
        self.registry = registry
        self.permission_overrides = dict(permission_overrides or {})

    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        approved: bool = False,
        arguments_override: dict[str, object] | None = None,
        signal: AbortSignal | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecutionOutcome:
        tool = self.registry.get(invocation.tool_name)
        effective_mode = self.permission_overrides.get(
            tool.definition.name, tool.definition.permission_mode
        )
        invocation = invocation.model_copy(
            update={
                "source": tool.definition.source,
                "permission_mode": effective_mode,
                "arguments": arguments_override
                if arguments_override is not None
                else invocation.arguments,
            }
        )
        if tool.definition.name == "AskUser":
            question = str(invocation.arguments.get("question", ""))
            result = ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                content=question,
                structured={"question": question},
                ended_at=utc_now(),
            )
            return ToolExecutionOutcome(
                status="waiting_user",
                invocation=invocation,
                result=result,
                question=question,
            )
        if invocation.permission_mode == "approval" and not approved:
            approval = ApprovalRecord(
                session_id=invocation.session_id,
                run_id=invocation.run_id,
                tool_call_id=invocation.tool_call_id,
            )
            invocation = invocation.model_copy(update={"approval_id": approval.approval_id})
            return ToolExecutionOutcome(
                status="approval_required",
                invocation=invocation,
                approval=approval,
            )
        context = context or ToolExecutionContext(signal=signal)
        try:
            result = await tool.execute(invocation, context=context)
        except ToolExecutionAborted as exc:
            result = ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                is_error=True,
                content=exc.reason,
                structured={"aborted": True, "reason": exc.reason},
                ended_at=utc_now(),
            )
            return ToolExecutionOutcome(status="aborted", invocation=invocation, result=result)
        except Exception as exc:  # noqa: BLE001
            # Tool handlers may raise arbitrary exceptions when interacting with
            # the filesystem, network, subprocesses, or third-party code. The
            # executor is the framework boundary that isolates a single tool
            # failure from the rest of the run so the model can observe the
            # error and decide how to proceed.
            error_type = type(exc).__name__
            error_message = str(exc)
            result = ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                is_error=True,
                content=f"Tool {invocation.tool_name} failed: [{error_type}] {error_message}",
                structured={
                    "error_type": error_type,
                    "error_message": error_message,
                },
                ended_at=utc_now(),
            )
            return ToolExecutionOutcome(status="completed", invocation=invocation, result=result)
        return ToolExecutionOutcome(status="completed", invocation=invocation, result=result)

    def rejected_result(
        self,
        invocation: ToolInvocation,
        reason: str = "User rejected",
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            is_error=True,
            content=reason,
            structured={"rejected": True, "reason": reason},
            ended_at=utc_now(),
        )
