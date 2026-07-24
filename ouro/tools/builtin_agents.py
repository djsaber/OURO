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
                    "List direct subagents spawned from the current Session/Run, "
                    "including their status, terminal status, and bounded result summaries.\n\n"
                    "Use this for:\n"
                    "- Inspecting the state of async subagents after receiving an "
                    "agent_result_ready event.\n"
                    "- Checking which subagents have finished before deciding the next step.\n"
                    "- Retrieving final_text or resource_refs from completed subagents.\n\n"
                    "Do not use this for tight polling; async results are delivered automatically "
                    "as a steer HostInput at the next parent model boundary."
                ),
                parameters_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
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
                    "Send a message to a subagent. The behavior depends on its state:\n\n"
                    "- Active subagent (has a running Run): the message must use "
                    "mode='steer' and is routed "
                    "into the current Run as a host input for real-time guidance.\n"
                    "- Idle/terminal subagent (no active Run): the message must use "
                    "mode='follow_up' and "
                    "starts a new Run continuing the same subagent Session.\n\n"
                    "Default mode is 'steer'. Only use 'follow_up' when the subagent "
                    "has finished its "
                    "current Run and you want to resume it with a new Run."
                ),
                parameters_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "delegation_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": "The delegation_id from the subagent's AgentHandle.",
                        },
                        "message": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 20_000,
                            "description": "Message content for the subagent.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["steer", "follow_up"],
                            "default": "steer",
                            "description": (
                                "'steer' injects into the active Run; 'follow_up' starts a new Run "
                                "after the subagent has finished."
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
                    "Interrupt the active Run of a subagent without deleting its history.\n\n"
                    "Use this to stop a long-running or off-track subagent. After interruption:\n"
                    "- The subagent's Session and message history are preserved.\n"
                    "- You may send follow_up messages later to resume from where it stopped.\n"
                    "- Any terminal result from the interrupted Run may still be delivered."
                ),
                parameters_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "delegation_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": "The delegation_id from the subagent's AgentHandle.",
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": "Optional reason recorded for the interruption.",
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
                "description": (
                    "The agent_id to spawn, selected from the frozen Agent catalog. "
                    "Each option describes the subagent's capabilities and tools."
                ),
            },
            "task": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20_000,
                "description": (
                    "Self-contained task instruction for the subagent. Include all "
                    "context it needs; "
                    "the subagent starts without access to the current conversation."
                ),
            },
            "description": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": "Short (3-5 word) summary of the task for UI and logs.",
            },
            "nickname": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": "Optional display name for the subagent's Session.",
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
                    "'sync' blocks until the subagent finishes and returns its result "
                    "as the tool result. 'async' returns a durable handle immediately; "
                    "the result is delivered automatically "
                    "as a steer HostInput at the next parent model boundary."
                ),
            },
            "resource_refs": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 16,
                "uniqueItems": True,
                "description": (
                    "Parent Session resource references to copy into the subagent Session. "
                    "Use only when the subagent needs access to specific parent resources."
                ),
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
                "Dispatch an independent subagent to perform a task.\n\n"
                "Execution modes:\n"
                "- 'sync': Blocks until the subagent finishes. The tool result contains "
                "the subagent's handle "
                "and final result.\n"
                "- 'async': Returns a durable handle immediately. The subagent runs in "
                "the background and the result is delivered automatically as a steer "
                "HostInput at the next parent model boundary.\n\n"
                "Important limits for the current release:\n"
                "- Subagent nesting depth is 1; a subagent cannot spawn further subagents.\n"
                "- Subagents use the ordinary runtime scheduler; tool permissions do not limit "
                "dispatch concurrency.\n\n"
                "Provide a self-contained task because the subagent starts without prior "
                "conversation context."
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
