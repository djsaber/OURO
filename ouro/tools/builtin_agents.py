from __future__ import annotations

from typing import Any

from ouro.core.agents import AgentCatalogSnapshot, AgentControlRequest
from ouro.core.tools import ToolInvocation, ToolResult
from ouro.tools.base import Tool, ToolDefinition


def agent_control_tools(catalog: AgentCatalogSnapshot) -> tuple[Tool, ...]:
    """Project one Run-frozen Agent catalog into product-owned tools."""

    options = [
        {
            "const": entry.definition.agent_id,
            "title": entry.definition.name,
            "description": entry.definition.description,
        }
        for entry in catalog.entries
        if entry.available
    ]
    spawn_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_id": {"oneOf": options},
            "task": {"type": "string", "minLength": 1, "maxLength": 20_000},
            "description": {"type": "string", "minLength": 1, "maxLength": 500},
            "nickname": {"type": "string", "minLength": 1, "maxLength": 80},
            "execution_mode": {
                "type": "string",
                "enum": ["foreground", "background"],
                "default": "background",
            },
            "delivery_policy": {
                "type": "string",
                "enum": ["notify_only", "next_boundary", "follow_up"],
                "default": "notify_only",
            },
        },
        "required": ["agent_id", "task", "description"],
        "additionalProperties": False,
    }
    metadata = {
        "ouro_agent_control": True,
        "agent_catalog_contract_hash": catalog.contract_hash,
    }
    return (
        Tool(
            definition=ToolDefinition(
                name="SpawnAgent",
                display_name="派遣子智能体",
                description=(
                    "Create an independent child Agent and return its durable handle immediately. "
                    "Use WaitAgents when the result is required; do not poll repeatedly."
                ),
                parameters_schema=spawn_schema,
                effect="state_changing",
                metadata=metadata,
            ),
            handler=_spawn,
        ),
        Tool(
            definition=ToolDefinition(
                name="WaitAgents",
                description=(
                    "Wait durably for one or more child Agents. The parent releases its runtime "
                    "worker while waiting and resumes with the results."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "delegation_ids": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                            "maxItems": 32,
                            "uniqueItems": True,
                        },
                        "completion_condition": {
                            "type": "string",
                            "enum": ["all", "any"],
                            "default": "all",
                        },
                        "timeout_seconds": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 86_400,
                        },
                    },
                    "required": ["delegation_ids"],
                    "additionalProperties": False,
                },
                effect="read_only",
                execution_mode="sequential",
                batch_behavior="exclusive",
                metadata=metadata,
            ),
            handler=_wait,
        ),
        Tool(
            definition=ToolDefinition(
                name="ListAgents",
                description=(
                    "List direct child Agents and bounded terminal result references. "
                    "Use this for explicit status inspection, not continuous polling."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                effect="read_only",
                metadata=metadata,
            ),
            handler=_list,
        ),
        Tool(
            definition=ToolDefinition(
                name="SendAgentMessage",
                description=(
                    "Send a synthetic Host message to a child Agent. Active children accept steer; "
                    "idle children accept a follow-up Run."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "delegation_id": {"type": "string", "minLength": 1},
                        "message": {"type": "string", "minLength": 1, "maxLength": 20_000},
                        "mode": {
                            "type": "string",
                            "enum": ["steer", "follow_up"],
                            "default": "steer",
                        },
                    },
                    "required": ["delegation_id", "message"],
                    "additionalProperties": False,
                },
                effect="state_changing",
                execution_mode="sequential",
                batch_behavior="exclusive",
                metadata=metadata,
            ),
            handler=_send,
        ),
        Tool(
            definition=ToolDefinition(
                name="InterruptAgent",
                description=(
                    "Interrupt the current Run of a child Agent without deleting its history."
                ),
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "delegation_id": {"type": "string", "minLength": 1},
                        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                    },
                    "required": ["delegation_id"],
                    "additionalProperties": False,
                },
                effect="state_changing",
                execution_mode="sequential",
                batch_behavior="exclusive",
                metadata=metadata,
            ),
            handler=_interrupt,
        ),
    )


def _intent_result(invocation: ToolInvocation, request: AgentControlRequest) -> ToolResult:
    return ToolResult(
        tool_call_id=invocation.tool_call_id,
        tool_name=invocation.tool_name,
        agent_control_request=request,
    )


def _spawn(invocation: ToolInvocation) -> ToolResult:
    arguments = invocation.arguments
    execution_mode = arguments.get("execution_mode", "background")
    delivery_policy = arguments.get("delivery_policy", "notify_only")
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
            delivery_policy=(
                "resume_waiter" if execution_mode == "foreground" else delivery_policy
            ),
        ),
    )


def _wait(invocation: ToolInvocation) -> ToolResult:
    arguments = invocation.arguments
    return _intent_result(
        invocation,
        AgentControlRequest(
            action="wait",
            request_id=_request_id(invocation),
            delegation_ids=tuple(str(item) for item in arguments["delegation_ids"]),
            completion_condition=arguments.get("completion_condition", "all"),
            timeout_seconds=arguments.get("timeout_seconds"),
        ),
    )


def _list(invocation: ToolInvocation) -> ToolResult:
    return _intent_result(
        invocation,
        AgentControlRequest(action="list", request_id=_request_id(invocation)),
    )


def _send(invocation: ToolInvocation) -> ToolResult:
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


def _interrupt(invocation: ToolInvocation) -> ToolResult:
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
