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
                name="SendAgentMessage",
                description=(
                    "Use this tool to give new information or instructions to an existing "
                    "subagent. Use mode='steer' while the subagent is actively working so the "
                    "message guides its current run. Use mode='follow_up' only after that run "
                    "has ended to continue the same subagent with a new run. Do not use "
                    "follow_up for an active subagent, steer for an idle subagent, or this tool "
                    "when the work belongs to a new independent subagent; use SpawnAgent for "
                    "the latter. Avoid sending duplicate guidance that the subagent already "
                    "received."
                ),
                parameters_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "delegation_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Exact delegation_id of the existing subagent you want to "
                                "message, taken from its handle or agent listing."
                            ),
                        },
                        "message": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 20_000,
                            "description": (
                                "New, actionable guidance for the subagent. Include any changed "
                                "requirements or evidence it needs to act correctly."
                            ),
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["steer", "follow_up"],
                            "default": "steer",
                            "description": (
                                "Use 'steer' for a subagent with an active run. Use 'follow_up' "
                                "only after its run has ended to start a continuation run. Omit "
                                "for 'steer'."
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
                    "Use this tool to stop an active subagent that is no longer needed, is "
                    "off-track, or is consuming resources without useful progress. Do not "
                    "interrupt an idle or completed subagent, and prefer SendAgentMessage with "
                    "mode='steer' when corrected guidance is sufficient. Interruption preserves "
                    "the subagent's history, so you can later resume it with a follow_up. A "
                    "result already produced at the interruption boundary may still arrive."
                ),
                parameters_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "delegation_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Exact delegation_id of the active subagent to stop, taken from "
                                "its handle or agent listing."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": (
                                "Concise reason for stopping the subagent, suitable for later "
                                "diagnosis. Omit when the reason is already obvious."
                            ),
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
                    "Choose the available agent_id whose stated capabilities best match the "
                    "delegated task."
                ),
            },
            "task": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20_000,
                "description": (
                    "Self-contained instruction describing the objective, relevant context, "
                    "constraints, expected deliverable, and verification. The subagent does "
                    "not receive the current conversation automatically."
                ),
            },
            "description": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": (
                    "Short 3-5 word task summary that distinguishes this delegation from others."
                ),
            },
            "nickname": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": (
                    "Optional short display name for this subagent. Use one when several "
                    "delegations would otherwise be hard to distinguish."
                ),
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
                    "Use 'sync' when you need the subagent's result before your next step. Use "
                    "'async' when you can continue independent work while it runs; async "
                    "returns a handle immediately and delivers the result automatically."
                ),
            },
            "resource_refs": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "maxItems": 16,
                "uniqueItems": True,
                "description": (
                    "IDs of resources from the current session that the subagent must inspect. "
                    "Omit resources unrelated to its task."
                ),
            },
        },
        "required": ["agent_id", "task", "description"],
        "additionalProperties": False,
    }
    available_agents = "\n".join(
        f"- id: {option['const']}, name: {option['title']}, description: {option['description']}"
        for option in options
    )
    return Tool(
        definition=ToolDefinition(
            name="SpawnAgent",
            display_name="派遣子智能体",
            description=(
                "Use this tool to delegate a bounded, self-contained task to an independent "
                "subagent when separate context, specialized capabilities, or parallel work "
                "will materially help. Do not spawn a subagent for trivial work, tightly "
                "coupled sequential steps, or tasks you can complete faster in the current "
                "context. Provide a complete task because the subagent does not inherit this "
                "conversation.\n\n"
                "Available subagents:\n"
                f"{available_agents}\n\n"
                "Choose 'sync' when its result is required before you continue. Choose 'async' "
                "when you have independent work to do; the result will be delivered "
                "automatically.\n\n"
                "Limits:\n"
                "- Subagent nesting depth is 1; a subagent cannot spawn further subagents.\n"
                "- Choose only an agent_id listed above; a parallel dispatch may wait when "
                "execution capacity is full."
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
