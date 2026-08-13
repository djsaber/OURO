from __future__ import annotations

from collections.abc import Mapping

from ouro.core.approvals import (
    ApprovalRecord,
    SessionApprovalMode,
    resolve_tool_permission,
    restrict_tool_permission,
)
from ouro.core.base import utc_now
from ouro.core.cancellation import AbortSignal
from ouro.core.interactions import ToolInputResolution
from ouro.core.redaction import redact_text
from ouro.core.tool_policies import ToolPolicyMode
from ouro.core.tools import PermissionMode
from ouro.tools.base import (
    ToolExecutionAborted,
    ToolExecutionContext,
    ToolExecutionOutcome,
    ToolInputRequest,
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
        permission_floors: Mapping[str, PermissionMode] | None = None,
    ) -> None:
        self.registry = registry
        self.permission_overrides = dict(permission_overrides or {})
        self.permission_floors = dict(permission_floors or {})

    def replace_permission_overrides(
        self,
        permission_overrides: Mapping[str, PermissionMode],
    ) -> None:
        """Publish the host permission policy used by subsequent preflight checks."""

        self.permission_overrides = dict(permission_overrides)

    async def execute(
        self,
        invocation: ToolInvocation,
        *,
        approved: bool = False,
        arguments_override: dict[str, object] | None = None,
        signal: AbortSignal | None = None,
        context: ToolExecutionContext | None = None,
        approval_mode: SessionApprovalMode = "inherit",
        policy_mode: ToolPolicyMode | None = None,
        policy_reason: str | None = None,
    ) -> ToolExecutionOutcome:
        prepared = self.preflight(
            invocation,
            approved=approved,
            arguments_override=arguments_override,
            approval_mode=approval_mode,
            policy_mode=policy_mode,
            policy_reason=policy_reason,
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
        policy_mode: ToolPolicyMode | None = None,
        policy_reason: str | None = None,
    ) -> ToolExecutionOutcome:
        try:
            tool = self.registry.get(invocation.tool_name)
        except KeyError:
            return _error_outcome(
                invocation,
                error_type="UnknownTool",
                error_message=f"unknown tool: {invocation.tool_name}",
            )
        contract_error = _tool_contract_error(invocation, tool.definition.metadata)
        if contract_error is not None:
            return _error_outcome(
                invocation,
                error_type="ToolExecutionContractMismatch",
                error_message=contract_error,
            )
        effective_mode = resolve_tool_permission(
            tool.definition.permission_mode,
            approval_mode,
            permission_control=tool.definition.permission_control,
            override=self.permission_overrides.get(tool.definition.name),
        )
        effective_mode = restrict_tool_permission(
            effective_mode,
            self.permission_floors.get(tool.definition.name, "auto"),
        )
        denial_reason: str | None = None
        if policy_mode == "deny":
            effective_mode = "deny"
            denial_reason = policy_reason
        elif policy_mode == "approval" and effective_mode == "auto":
            effective_mode = "approval"
        invocation = invocation.model_copy(
            update={
                "source": tool.definition.source,
                "effect": tool.definition.effect,
                "batch_behavior": tool.definition.batch_behavior,
                "permission_mode": effective_mode,
                "permission_control": tool.definition.permission_control,
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
            reason = denial_reason or (
                f"tool is disabled by permission policy: {invocation.tool_name}"
            )
            return _error_outcome(
                invocation,
                error_type="ToolPermissionDenied",
                error_message=reason,
            )
        if (
            invocation.permission_mode == "approval"
            and tool.definition.kind != "user_input"
            and not approved
        ):
            approval = ApprovalRecord(
                session_id=invocation.session_id,
                run_id=invocation.run_id,
                tool_call_id=invocation.tool_call_id,
                execution_contract_hash=invocation.execution_contract_hash,
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
            execution_result = await tool.execute(invocation, context=context)
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
        if isinstance(execution_result, ToolInputRequest):
            return ToolExecutionOutcome(
                status="waiting_user",
                invocation=invocation,
                input_request=execution_result,
            )
        result = _validated_tool_result(invocation, execution_result, context)
        return ToolExecutionOutcome(status="completed", invocation=invocation, result=result)

    async def resume(
        self,
        invocation: ToolInvocation,
        *,
        resolution: ToolInputResolution,
        request: ToolInputRequest,
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
        contract_error = _tool_contract_error(invocation, tool.definition.metadata)
        if contract_error is not None:
            return _error_outcome(
                invocation,
                error_type="ToolExecutionContractMismatch",
                error_message=contract_error,
            )
        if not tool.definition.resumable or tool.resume_handler is None:
            return _error_outcome(
                invocation,
                error_type="ToolResumeUnsupported",
                error_message=f"tool does not support resume: {invocation.tool_name}",
            )
        context = context or ToolExecutionContext(signal=signal)
        try:
            context.raise_if_aborted()
            execution_result = await tool.resume(
                invocation,
                resolution,
                request=request,
                context=context,
            )
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
        except Exception as exc:  # noqa: BLE001 - tool resume is an extension boundary.
            error_type = type(exc).__name__
            error_message = redact_text(str(exc))
            result = ToolResult(
                tool_call_id=invocation.tool_call_id,
                tool_name=invocation.tool_name,
                is_error=True,
                content=(
                    f"Tool {invocation.tool_name} resume failed: [{error_type}] {error_message}"
                ),
                structured={
                    "error_type": error_type,
                    "error_message": error_message,
                },
                ended_at=utc_now(),
            )
            return ToolExecutionOutcome(status="completed", invocation=invocation, result=result)
        if isinstance(execution_result, ToolInputRequest):
            return ToolExecutionOutcome(
                status="waiting_user",
                invocation=invocation,
                input_request=execution_result,
            )
        result = _validated_tool_result(invocation, execution_result, context)
        return ToolExecutionOutcome(status="completed", invocation=invocation, result=result)


def _error_outcome(
    invocation: ToolInvocation,
    *,
    error_type: str,
    error_message: str,
) -> ToolExecutionOutcome:
    result = _error_result(
        invocation,
        error_type=error_type,
        error_message=error_message,
    )
    return ToolExecutionOutcome(status="completed", invocation=invocation, result=result)


def _error_result(
    invocation: ToolInvocation,
    *,
    error_type: str,
    error_message: str,
) -> ToolResult:
    return ToolResult(
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


def _validated_tool_result(
    invocation: ToolInvocation,
    value: object,
    context: ToolExecutionContext,
) -> ToolResult:
    """Keep extension output inside the invocation that owns its protocol id."""

    if not isinstance(value, ToolResult):
        return _error_result(
            invocation,
            error_type="ToolResultContractError",
            error_message=(
                "tool handler must return ToolResult or ToolInputRequest, "
                f"not {type(value).__name__}"
            ),
        )
    if value.tool_call_id != invocation.tool_call_id or value.tool_name != invocation.tool_name:
        return _error_result(
            invocation,
            error_type="ToolResultContractError",
            error_message="tool result identity does not match the current invocation",
        )
    content, truncated = context.limit_output(value.content)
    if not truncated:
        return value
    return value.model_copy(
        update={
            "content": content,
            "metadata": {**value.metadata, "output_truncated": True},
        }
    )


def _tool_contract_error(
    invocation: ToolInvocation,
    metadata: Mapping[str, object],
) -> str | None:
    expected_common = {
        "plugin_id": invocation.plugin_id,
        "plugin_class": invocation.plugin_class,
        "release_id": invocation.release_id,
        "contract_hash": invocation.contract_hash,
        "execution_contract_hash": invocation.execution_contract_hash,
    }
    if not any(value is not None for value in expected_common.values()):
        return None
    for field, expected_value in expected_common.items():
        actual = metadata.get(field)
        if expected_value is None or actual != expected_value:
            return f"persisted {field} does not match the active tool contract"
    if invocation.plugin_class == "user":
        if (
            invocation.package_digest is None
            or metadata.get("package_digest") != invocation.package_digest
        ):
            return "persisted package_digest does not match the active tool contract"
    elif invocation.plugin_class == "builtin":
        if (
            invocation.builtin_contract_hash is None
            or metadata.get("builtin_contract_hash") != invocation.builtin_contract_hash
        ):
            return "persisted builtin_contract_hash does not match the active tool contract"
    else:
        return "persisted plugin_class does not identify a supported plugin class"
    return None
