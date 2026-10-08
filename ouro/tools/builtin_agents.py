from __future__ import annotations

from typing import Any

from ouro.core.agents import AgentCatalogSnapshot, AgentControlRequest
from ouro.core.tools import ToolInvocation, ToolResult
from ouro.tools.base import Tool, ToolDefinition, ToolExecutionContext


def agent_control_tools(catalog: AgentCatalogSnapshot) -> tuple[Tool, ...]:
    """Project one decision-bound Agent catalog into product-owned tools."""

    options = [
        {
            "const": entry.definition.agent_id,
            "title": entry.definition.name,
            "description": entry.definition.description,
        }
        for entry in catalog.entries
        if entry.available
    ]
    metadata = {
        "ouro_agent_control": True,
        "agent_catalog_contract_hash": catalog.contract_hash,
    }
    controls = (
        Tool(
            definition=ToolDefinition(
                name="SendAgentMessage",
                description=(
                    "Send new guidance to an existing subagent or continue its work. Use "
                    "SpawnAgent for a new independent task. Avoid duplicate messages."
                ),
                parameters_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "delegation_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Existing subagent's delegation_id from a previous result "
                                "or agent listing."
                            ),
                        },
                        "message": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 20_000,
                            "description": (
                                "Actionable guidance, including changed requirements or evidence."
                            ),
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["steer", "follow_up"],
                            "default": "steer",
                            "description": (
                                "Use 'steer' only to guide an actively working subagent; "
                                "'follow_up' continues it only after its current work has ended."
                            ),
                        },
                    },
                    "required": ["delegation_id", "message"],
                    "additionalProperties": False,
                },
                effect="state_changing",
                metadata=metadata,
            ),
            handler=_send,
        ),
        Tool(
            definition=ToolDefinition(
                name="InterruptAgent",
                description=(
                    "Stop an active subagent when its work is unneeded, off track, or "
                    "unproductive. "
                    "Prefer SendAgentMessage with mode='steer' if guidance is sufficient. "
                    "Do not interrupt idle or completed subagents. History is preserved for "
                    "later follow_up; an already-produced result may still arrive."
                ),
                parameters_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "delegation_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Active subagent's delegation_id from a previous result "
                                "or agent listing."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": "Brief reason for stopping; omit if obvious.",
                        },
                    },
                    "required": ["delegation_id"],
                    "additionalProperties": False,
                },
                effect="state_changing",
                metadata=metadata,
            ),
            handler=_interrupt,
        ),
    )
    if not options:
        return controls
    return (_spawn_control_tool(catalog, options, metadata), *controls)


def _spawn_control_tool(
    catalog: AgentCatalogSnapshot,
    options: list[dict[str, str]],
    metadata: dict[str, object],
) -> Tool:
    spawn_schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "agent_id": {
                "oneOf": options,
                "description": "Choose from the listed agents by capability.",
            },
            "task": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20_000,
                "description": (
                    "Self-contained objective, context, constraints, deliverable, and "
                    "verification steps. The subagent does not inherit this conversation."
                ),
            },
            "description": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "Distinctive 3-5 word task summary.",
            },
            "nickname": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": "Short display name to distinguish this subagent.",
            },
            "execution_mode": {
                "type": "string",
                "enum": list(catalog.spawn_policy.allowed_execution_modes),
                "default": (
                    "sync"
                    if "sync" in catalog.spawn_policy.allowed_execution_modes
                    else catalog.spawn_policy.allowed_execution_modes[0]
                ),
                "description": (
                    "Use 'sync' when the result is needed before your next step; it waits. "
                    "'async' returns delegation_id immediately "
                    "and delivers the result automatically; use while doing independent work."
                ),
            },
            "resource_refs": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 16,
                "uniqueItems": True,
                "description": "IDs of resources in this conversation needed for the task.",
            },
        },
        "required": ["agent_id", "task", "description"],
        "additionalProperties": False,
    }
    return Tool(
        definition=ToolDefinition(
            name="SpawnAgent",
            display_name="派遣子智能体",
            description=(
                "Delegate a bounded task when separate context, specialized capabilities, "
                "or parallel work helps. Avoid trivial tasks, tightly coupled steps, or work "
                "you can finish faster yourself. Available agents are listed under agent_id. "
                "Subagents cannot spawn further subagents; work may queue when capacity is full."
            ),
            parameters_schema=spawn_schema,
            effect="state_changing",
            metadata=metadata,
        ),
        handler=_spawn,
    )


def _intent_result(invocation: ToolInvocation, request: AgentControlRequest) -> ToolResult:
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        agent_control_request=request,
    )


def _spawn(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
    arguments = invocation.arguments
    execution_mode = arguments.get("execution_mode", "sync")
    return _intent_result(
        invocation,
        AgentControlRequest(
            action="spawn",
            request_id=_request_id(invocation),
            agent_id=str(arguments["agent_id"]),
            task=str(arguments["task"]),
            description=str(arguments["description"]),
            nickname=_optional_string(arguments.get("nickname")),
            execution_mode=execution_mode,
            resource_refs=tuple(str(item) for item in arguments.get("resource_refs", ())),
        ),
    )


def _send(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
    arguments = invocation.arguments
    return _intent_result(
        invocation,
        AgentControlRequest(
            action="send",
            request_id=_request_id(invocation),
            delegation_id=str(arguments["delegation_id"]),
            message=str(arguments["message"]),
            input_mode=arguments.get("mode", "steer"),
        ),
    )


def _interrupt(invocation: ToolInvocation, context: ToolExecutionContext) -> ToolResult:
    arguments = invocation.arguments
    return _intent_result(
        invocation,
        AgentControlRequest(
            action="interrupt",
            request_id=_request_id(invocation),
            delegation_id=str(arguments["delegation_id"]),
            reason=_optional_string(arguments.get("reason")),
        ),
    )


def _request_id(invocation: ToolInvocation) -> str:
    return f"{invocation.run_id}/{invocation.tool_call_id}"


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = ["agent_control_tools"]
