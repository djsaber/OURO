from __future__ import annotations

from collections.abc import Mapping

from ouro.core.approvals import ApprovalRecord, SessionApprovalMode
from ouro.core.base import utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.redaction import redact_text
from ouro.core.tools import PermissionMode
from ouro.tools.base import (
    ToolExecutionAborted,
    ToolExecutionContext,
    ToolExecutionOutcome,
    ToolInvocation,
    ToolResult,
)
from ouro.tools.registry import ToolRegistry
from ouro.tools.schema import tool_argument_error


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
        approval_mode: SessionApprovalMode = "inherit",
    ) -> ToolExecutionOutcome:
        prepared = self.preflight(
            invocation,
            approved=approved,
            arguments_override=arguments_override,
            approval_mode=approval_mode,
        )
        if prepared.status != "ready":
            return prepared
        invocation = prepared.invocation
        return await self.execute_prepared(invocation, signal=signal, context=context)

    def preflight(
        self,
        invocation: ToolInvocation,
        *,
        approved: bool = False,
        arguments_override: dict[str, object] | None = None,
        approval_mode: SessionApprovalMode = "inherit",
    ) -> ToolExecutionOutcome:
        try:
            tool = self.registry.get(invocation.tool_name)
        except KeyError:
            return _error_outcome(
                invocation,
                error_type="UnknownTool",
                error_message=f"unknown tool: {invocation.tool_name}",
            )
        effective_mode = self.permission_overrides.get(
            tool.definition.name, tool.definition.permission_mode
        )
        if effective_mode != "deny" and approval_mode != "inherit":
            effective_mode = approval_mode
        invocation = invocation.model_copy(
            update={
                "source": tool.definition.source,
                "permission_mode": effective_mode,
                "arguments": arguments_override
                if arguments_override is not None
                else invocation.arguments,
            }
        )
        argument_error = tool_argument_error(tool.definition, invocation.arguments)
        if argument_error is not None:
            return _error_outcome(
                invocation,
                error_type="ToolArgumentValidationError",
                error_message=argument_error,
            )
        if invocation.permission_mode == "deny":
            return _error_outcome(
                invocation,
                error_type="ToolPermissionDenied",
                error_message=f"tool is disabled by permission policy: {invocation.tool_name}",
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
        return ToolExecutionOutcome(status="ready", invocation=invocation)

    async def execute_prepared(
        self,
        invocation: ToolInvocation,
        *,
        signal: AbortSignal | None = None,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecutionOutcome:
        try:
            tool = self.registry.get(invocation.tool_name)
        except KeyError:
            return _error_outcome(
                invocation,
                error_type="UnknownTool",
                error_message=f"unknown tool: {invocation.tool_name}",
            )
        context = context or ToolExecutionContext(signal=signal)
        try:
            context.raise_if_aborted()
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
            error_message = redact_text(str(exc))
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
        content, truncated = context.limit_output(result.content)
        if truncated:
            result = result.model_copy(
                update={
                    "content": content,
                    "metadata": {**result.metadata, "output_truncated": True},
                }
            )
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


def _error_outcome(
    invocation: ToolInvocation,
    *,
    error_type: str,
    error_message: str,
) -> ToolExecutionOutcome:
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
