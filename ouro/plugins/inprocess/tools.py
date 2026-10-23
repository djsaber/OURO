"""Adapt SDK tool definitions and handlers to host tools."""

from __future__ import annotations

from types import MappingProxyType

from ouro.core.interactions import AnswerResolution, ToolInputResolution, UnattendedResolution
from ouro.core.session_controls import SessionControlTransitionRequest
from ouro.plugins.harness_calls import bind_context_callback, invoke_plugin_callback
from ouro.plugins.tool_content import from_sdk_content
from ouro.sdk.plugins import v5 as sdk
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolInputRequest,
    ToolInvocation,
    ToolResult,
)


def _adapt_tool(contribution: sdk.ToolContribution, *, owner_id: str) -> Tool:
    execute_callback = bind_context_callback(contribution.handler)
    resume_callback = (
        bind_context_callback(contribution.resume_handler)
        if contribution.resume_handler is not None
        else None
    )

    async def handler(
        invocation: ToolInvocation,
        context: ToolExecutionContext,
    ) -> ToolResult | ToolInputRequest:
        call = sdk.ToolCall(
            tool_call_id=invocation.tool_call_id,
            session_id=invocation.session_id,
            run_id=invocation.run_id,
            tool_name=invocation.tool_name,
            arguments=MappingProxyType(dict(invocation.arguments)),
            workspace=str(context.workspace) if context.workspace is not None else None,
            workspace_only=context.workspace_only,
        )
        raw, change = await invoke_plugin_callback(
            execute_callback,
            call,
            snapshot=context.harness,
            owner_id=owner_id,
            writable=contribution.effect != "read_only",
            signal=context.signal,
            context=_SDKToolExecutionContext(context),
        )
        if isinstance(raw, str):
            raw = sdk.ToolOutput(content=raw)
        if isinstance(raw, sdk.ToolInputRequest):
            return ToolInputRequest(data=raw.data, harness_change=change)
        return ToolResult(
            harness_change=change,
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=from_sdk_content(raw.content),
            structured=dict(raw.structured),
            resource_refs=list(raw.resource_refs),
            is_error=raw.is_error,
            session_control_transitions=[
                SessionControlTransitionRequest(
                    action=item.action,
                    controller_id=item.controller_id,
                    activation_id=item.activation_id,
                    replace_activation_ids=list(item.replace_activation_ids),
                    expected_session_revision=item.expected_session_revision,
                    expected_activation_revision=item.expected_activation_revision,
                    payload=item.payload,
                    reason=item.reason,
                )
                for item in raw.session_control_transitions
            ],
        )

    async def resume_handler(
        invocation: ToolInvocation,
        resolution: ToolInputResolution,
        request: ToolInputRequest,
        context: ToolExecutionContext,
    ) -> ToolResult | ToolInputRequest:
        if resume_callback is None:
            raise RuntimeError(f"tool does not support resume: {contribution.exposed_name}")
        call = sdk.ToolResumeCall(
            tool_call_id=invocation.tool_call_id,
            session_id=invocation.session_id,
            run_id=invocation.run_id,
            tool_name=invocation.tool_name,
            arguments=MappingProxyType(dict(invocation.arguments)),
            request=request.data,
            resolution=_sdk_input_resolution(resolution),
            workspace=str(context.workspace) if context.workspace is not None else None,
            workspace_only=context.workspace_only,
        )
        raw, change = await invoke_plugin_callback(
            resume_callback,
            call,
            snapshot=context.harness,
            owner_id=owner_id,
            writable=contribution.effect != "read_only",
            signal=context.signal,
            context=_SDKToolExecutionContext(context),
        )
        if isinstance(raw, str):
            raw = sdk.ToolOutput(content=raw)
        if isinstance(raw, sdk.ToolInputRequest):
            return ToolInputRequest(data=raw.data, harness_change=change)
        return ToolResult(
            harness_change=change,
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=from_sdk_content(raw.content),
            structured=dict(raw.structured),
            resource_refs=list(raw.resource_refs),
            is_error=raw.is_error,
            session_control_transitions=[
                SessionControlTransitionRequest(
                    action=item.action,
                    controller_id=item.controller_id,
                    activation_id=item.activation_id,
                    replace_activation_ids=list(item.replace_activation_ids),
                    expected_session_revision=item.expected_session_revision,
                    expected_activation_revision=item.expected_activation_revision,
                    payload=item.payload,
                    reason=item.reason,
                )
                for item in raw.session_control_transitions
            ],
        )

    return Tool(
        definition=ToolDefinition(
            name=contribution.exposed_name,
            description=contribution.description,
            parameters_schema=dict(contribution.parameters_schema),
            result_schema=dict(contribution.result_schema),
            permission_mode=contribution.permission_mode,
            permission_control=contribution.permission_control,
            effect=contribution.effect,
            batch_behavior=contribution.batch_behavior,
            resumable=contribution.resume_handler is not None,
            supports_unattended=contribution.supports_unattended,
            active_when_controller_id=contribution.active_when,
            interaction_controller_id=contribution.interaction_controller,
            metadata={"sdk_contribution_id": contribution.id},
        ),
        handler=handler,
        resume_handler=resume_handler if contribution.resume_handler is not None else None,
    )


def _sdk_input_resolution(resolution: ToolInputResolution) -> sdk.ToolInputResolution:
    if isinstance(resolution, AnswerResolution):
        return sdk.AnswerResolution(
            value=resolution.value,
            resolved_by=resolution.resolved_by,
            resolver_id=resolution.resolver_id,
        )
    if isinstance(resolution, UnattendedResolution):
        return sdk.UnattendedResolution(
            resolver_id=resolution.resolver_id,
            reason=resolution.reason,
        )
    raise TypeError(f"unsupported tool input resolution: {type(resolution).__name__}")


class _SDKToolExecutionContext:
    def __init__(self, context: ToolExecutionContext) -> None:
        self._context = context

    @property
    def workspace(self) -> str | None:
        value = self._context.workspace
        return str(value) if value is not None else None

    @property
    def workspace_only(self) -> bool:
        return self._context.workspace_only

    @property
    def is_aborted(self) -> bool:
        return self._context.is_aborted

    @property
    def abort_reason(self) -> str:
        return self._context.abort_reason

    def raise_if_aborted(self) -> None:
        self._context.raise_if_aborted()

    async def sleep(self, seconds: float) -> None:
        await self._context.sleep(seconds)

    def resolve_path(self, path: str) -> str:
        return str(self._context.resolve_path(path))

    def resolve_working_directory(self, cwd: str | None = None) -> str | None:
        resolved = self._context.resolve_working_directory(cwd)
        return str(resolved) if resolved is not None else None
