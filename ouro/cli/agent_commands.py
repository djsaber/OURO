from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ouro.application.contracts.agents import SpawnAgentRequest
from ouro.cli.errors import error_exit_code
from ouro.core.agents import ToolSelection
from ouro.core.errors import ConfigError, OuroError
from ouro.local_application import LocalApplication, open_local_agent_definitions


async def run_agent_command(args: argparse.Namespace) -> int:
    """Dispatch the Host-facing Agent CLI without owning Agent state transitions."""

    try:
        if args.agent_command == "definition":
            payload, text = await _definition_command(args)
        else:
            payload, text = await _runtime_command(args)
    except (OuroError, FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
        _print_error(exc, json_mode=args.json)
        return _error_exit_code(exc)
    _print_success(payload, text=text, json_mode=args.json)
    return 0


async def _definition_command(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    async with open_local_agent_definitions(
        args.home,
        session_backend=args.command_session_backend,
    ) as services:
        definitions = services.definitions
        tools = services.tools
        command = args.definition_command
        if command == "list":
            catalog = definitions.list(tools=tools)
            return (
                {"catalog": catalog.model_dump(mode="json")},
                _definition_catalog_text(catalog.model_dump(mode="json")),
            )
        if command == "show":
            definition = definitions.get(args.agent_id, tools=tools)
            return (
                {"definition": definition.model_dump(mode="json")},
                json.dumps(definition.model_dump(mode="json"), ensure_ascii=False, indent=2),
            )
        if command == "tools":
            options = definitions.tool_options(tools)
            dumped_options = [item.model_dump(mode="json") for item in options]
            return ({"tools": dumped_options}, _tool_options_text(dumped_options))
        if command == "create":
            catalog = definitions.create(
                agent_id=args.agent_id,
                name=args.name,
                description=args.description,
                system_prompt=args.system_prompt,
                tool_selection=_tool_selection(args),
                model=args.model,
                tools=tools,
                expected_revision=args.expected_revision,
            )
        elif command == "update":
            current = definitions.get(args.agent_id, tools=tools)
            catalog = definitions.update(
                agent_id=args.agent_id,
                name=args.name,
                description=args.description,
                system_prompt=args.system_prompt,
                tool_selection=_tool_selection(args),
                model=(
                    None
                    if args.session_model
                    else args.model
                    if args.model is not None
                    else current.model
                ),
                tools=tools,
                expected_revision=args.expected_revision,
            )
        elif command == "delete":
            catalog = definitions.delete(
                args.agent_id,
                expected_revision=args.expected_revision,
                tools=tools,
            )
        elif command == "copy":
            catalog = definitions.copy_builtin(
                args.agent_id,
                new_agent_id=args.new_agent_id,
                name=args.name,
                tools=tools,
                expected_revision=args.expected_revision,
            )
        else:
            raise AssertionError(f"unknown agent definition command: {command}")
        dumped_catalog = catalog.model_dump(mode="json")
        return ({"catalog": dumped_catalog}, _definition_catalog_text(dumped_catalog))


async def _runtime_command(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    application = LocalApplication.build(
        args.home,
        session_backend=args.command_session_backend,
        task_backend=args.task_backend,
    )
    async with application:
        agents = application.agents
        if agents is None:
            raise RuntimeError("Agent controls are unavailable")
        command = args.agent_command
        if command in {"list", "tree", "interactions"}:
            await application.runtime.reconcile_session(args.root_session_id)
        elif command == "spawn":
            await application.runtime.reconcile_session(args.parent_session_id)
        elif command == "wait":
            for root_session_id in agents.delegation_root_session_ids(args.delegation_ids):
                await application.runtime.reconcile_session(root_session_id)
        else:
            root_session_id = agents.delegation_root_session_ids((args.delegation_id,))[0]
            await application.runtime.reconcile_session(root_session_id)
        if command == "list":
            handles = agents.list_agents(
                args.root_session_id,
                parent_session_id=args.parent_session_id,
            )
            dumped_handles = [item.model_dump(mode="json") for item in handles]
            return ({"agents": dumped_handles}, _agent_handles_text(dumped_handles))
        if command == "show":
            detail = agents.read_agent(args.delegation_id)
            dumped_detail = detail.model_dump(mode="json")
            return (
                {"agent": dumped_detail},
                json.dumps(dumped_detail, ensure_ascii=False, indent=2),
            )
        if command == "spawn":
            handle = await agents.spawn_agent(
                SpawnAgentRequest(
                    request_id=args.request_id,
                    parent_session_id=args.parent_session_id,
                    parent_run_id=args.parent_run_id,
                    agent_id=args.agent_id,
                    task=args.task,
                    description=args.description,
                    nickname=args.nickname,
                    execution_mode="sync",
                    resource_refs=tuple(args.resource_ids),
                    unattended=True,
                )
            )
            waited = await agents.wait_agents((handle.delegation_id,))
            handle = waited.handles[0]
            dumped_handle = handle.model_dump(mode="json")
            return ({"agent": dumped_handle}, _agent_handle_text(dumped_handle))
        if command == "wait":
            result = await agents.wait_agents(args.delegation_ids)
            dumped_wait = result.model_dump(mode="json")
            return ({"wait": dumped_wait}, _agent_handles_text(dumped_wait["handles"]))
        if command == "send":
            handle = await agents.send_agent_message(
                args.delegation_id,
                args.message,
                request_id=args.request_id,
                mode=args.mode,
            )
            if args.mode == "follow_up":
                handle = (await agents.wait_agents((handle.delegation_id,))).handles[0]
            dumped_handle = handle.model_dump(mode="json")
            return ({"agent": dumped_handle}, _agent_handle_text(dumped_handle))
        if command == "interrupt":
            handle = await agents.interrupt_agent(
                args.delegation_id,
                reason=args.reason,
            )
            dumped_handle = handle.model_dump(mode="json")
            return ({"agent": dumped_handle}, _agent_handle_text(dumped_handle))
        if command == "close":
            handle = await agents.close_agent(args.delegation_id)
            dumped_handle = handle.model_dump(mode="json")
            return ({"agent": dumped_handle}, _agent_handle_text(dumped_handle))
        if command == "tree":
            snapshot = agents.agent_tree_snapshot(args.root_session_id)
            dumped_tree = snapshot.model_dump(mode="json")
            return (
                {"tree": dumped_tree},
                json.dumps(dumped_tree, ensure_ascii=False, indent=2),
            )
        if command == "interactions":
            interactions = agents.list_agent_interactions(args.root_session_id)
            dumped_interactions = [item.model_dump(mode="json") for item in interactions]
            return (
                {"interactions": dumped_interactions},
                json.dumps(dumped_interactions, ensure_ascii=False, indent=2),
            )
        raise AssertionError(f"unknown agent command: {command}")


def _tool_selection(args: argparse.Namespace) -> ToolSelection:
    if args.all_tools and args.tool_names:
        raise ConfigError("--all-tools cannot be combined with --tool")
    return (
        ToolSelection(mode="all")
        if args.all_tools
        else ToolSelection(mode="selected", names=tuple(args.tool_names))
    )


def _print_success(payload: dict[str, Any], *, text: str, json_mode: bool) -> None:
    if json_mode:
        print(json.dumps({"status": "ok", **payload}, ensure_ascii=False))
    else:
        print(text)


def _print_error(exc: Exception, *, json_mode: bool) -> None:
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"Error: {exc}", file=sys.stderr)


def _error_exit_code(exc: Exception) -> int:
    if isinstance(exc, ConfigError):
        return 2
    if isinstance(exc, OuroError):
        return error_exit_code(exc)
    return 1


def _definition_catalog_text(catalog: dict[str, Any]) -> str:
    definitions = catalog["definitions"]
    if not definitions:
        return "No agent definitions."
    lines = [f"Agent definitions (revision {catalog['revision']}):"]
    for item in definitions:
        lines.append(
            f"- {item['agent_id']} {item['name']} [{item['source']}] [{item['compatibility']}]"
        )
    return "\n".join(lines)


def _tool_options_text(options: list[dict[str, Any]]) -> str:
    if not options:
        return "No Agent tools available."
    return "\n".join(
        ["Agent tool options:", *(f"- {item['name']} [{item['effect']}]" for item in options)]
    )


def _agent_handle_text(handle: dict[str, Any]) -> str:
    return (
        f"{handle['delegation_id']} {handle['status']} "
        f"agent={handle['agent_id']} revision={handle['revision']}"
    )


def _agent_handles_text(handles: list[dict[str, Any]]) -> str:
    if not handles:
        return "No agents."
    return "\n".join(["Agents:", *(_agent_handle_text(item) for item in handles)])


__all__ = ["run_agent_command"]
