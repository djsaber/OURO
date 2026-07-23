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
    metadata = {
        "ouro_agent_control": True,
        "agent_catalog_contract_hash": catalog.contract_hash,
    }
    controls = (
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
    if not options:
        return controls
    return (_spawn_control_tool(catalog, options, metadata), *controls)


def _spawn_control_tool(
    catalog: AgentCatalogSnapshot,
    options: list[dict[str, str]],
    metadata: dict[str, object],
) -> Tool:
    spawn_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent_id": {"oneOf": options},
            "task": {"type": "string", "minLength": 1, "maxLength": 20_000},
            "description": {"type": "string", "minLength": 1, "maxLength": 500},
            "nickname": {"type": "string", "minLength": 1, "maxLength": 80},
            "execution_mode": {
                "type": "string",
                "enum": list(catalog.spawn_policy.allowed_execution_modes),
                "default": (
                    "sync"
                    if "sync" in catalog.spawn_policy.allowed_execution_modes
                    else catalog.spawn_policy.allowed_execution_modes[0]
                ),
            },
            "context_message_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 200,
                "uniqueItems": True,
                "description": (
                    "Explicit parent message IDs for an Agent configured with "
                    "selected_messages context."
                ),
            },
            "resource_refs": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 16,
                "uniqueItems": True,
                "description": "Explicit parent Session resources delegated to the child.",
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
                "Dispatch an independent child Agent. In sync mode this call completes only "
                "after the child finishes and returns its result. In async mode it returns a "
                "durable handle immediately and the result is delivered automatically."
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


def _spawn(invocation: ToolInvocation) -> ToolResult:
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
            context_message_ids=tuple(
                str(item) for item in arguments.get("context_message_ids", ())
            ),
            resource_refs=tuple(str(item) for item in arguments.get("resource_refs", ())),
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
