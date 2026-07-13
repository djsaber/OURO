from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ouro.application.events import RuntimeEventSink
from ouro.application.models import ConfiguredModelCatalog
from ouro.application.queued_inputs import QueuedInputMode
from ouro.application.resources import ResourceService
from ouro.application.runtime import RuntimeService
from ouro.cli.errors import error_exit_code
from ouro.config import (
    CompactionConfig,
    CompactionConfigManager,
    PermissionConfigManager,
    PromptConfigManager,
    ProviderConfigManager,
    ProviderRuntimeOptions,
    QueuedInputConfigManager,
    RunBudgetConfigManager,
    ToolExecutionConfigManager,
)
from ouro.core.adapter import RuntimeAdapter
from ouro.core.context import ContextBuilder
from ouro.core.errors import OuroError
from ouro.core.results import RunResult
from ouro.mcp import MCPRuntimeResource
from ouro.prompts import render_agent_skills_prompt
from ouro.providers import build_runtime_adapter
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore
from ouro.skills import discover_available_skills
from ouro.tools import ToolRegistry, builtin_tools

RuntimeProviderOptions = ProviderRuntimeOptions

_DIM_ITALIC = "\x1b[2;3m"
_RESET = "\x1b[0m"


def build_default_runtime(
    home: str | Path | None = None,
    *,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
    event_sink: RuntimeEventSink | None = None,
) -> RuntimeService:
    store = LocalSessionStore(home)
    store.initialize()
    resource_store = LocalResourceStore(store.root)
    resource_store.initialize()
    resources = ResourceService(store=resource_store, sessions=store)
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    provider_config = ProviderConfigManager(store.root)
    resolved_options = provider_config.resolve_runtime_options(
        provider_options or RuntimeProviderOptions(),
    )
    adapter = build_runtime_adapter(resolved_options)
    model_catalog = ConfiguredModelCatalog(
        provider_config,
        adapter,
        adapter_builder=build_runtime_adapter,
    )
    skills_prompt = render_agent_skills_prompt(discover_available_skills(home=store.root))
    context_builder = ContextBuilder(skill_instructions=skills_prompt)
    resolved_system_prompt = PromptConfigManager(store.root).resolve_system_prompt(system_prompt)
    permission_overrides = PermissionConfigManager(store.root).snapshot().tools
    compaction_config = CompactionConfigManager(store.root).snapshot()
    tool_execution_config = ToolExecutionConfigManager(store.root).snapshot()
    run_budget = RunBudgetConfigManager(store.root).snapshot()
    queued_input_config = QueuedInputConfigManager(store.root).snapshot()
    compaction_adapter, compaction_adapter_error = _configured_compaction_adapter(
        provider_config,
        compaction_config,
    )
    return RuntimeService(
        store=store,
        adapter=adapter,
        model_catalog=model_catalog,
        tools=registry,
        context_builder=context_builder,
        system_prompt=resolved_system_prompt,
        event_sink=event_sink,
        permission_overrides=permission_overrides,
        tool_execution_config=tool_execution_config,
        run_budget=run_budget,
        compaction_config=compaction_config,
        compaction_adapter=compaction_adapter,
        compaction_adapter_error=compaction_adapter_error,
        queued_input_config=queued_input_config,
        resources=resources,
    )


def build_default_mcp_runtime(runtime: RuntimeService) -> MCPRuntimeResource:
    """Load configured MCP servers into the registry used by this runtime."""

    return MCPRuntimeResource.from_config(runtime.store.root, runtime.tools)


def _configured_compaction_adapter(
    providers: ProviderConfigManager,
    config: CompactionConfig,
) -> tuple[RuntimeAdapter | None, str | None]:
    model = config.model
    if not model.has_dedicated_endpoint:
        return None, None
    assert model.provider_id is not None
    assert model.endpoint_id is not None
    try:
        options = providers.resolve_runtime_options(
            ProviderRuntimeOptions(
                provider_id=model.provider_id,
                endpoint_id=model.endpoint_id,
            )
        )
        return build_runtime_adapter(options), None
    except Exception as exc:  # noqa: BLE001 - optional route must degrade to the runtime route.
        return (
            None,
            f"configured endpoint unavailable: {type(exc).__name__}: {exc}",
        )


async def run_print(
    prompt: str,
    *,
    home: str | Path | None = None,
    session_id: str | None = None,
    title: str | None = None,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
    queue_mode: QueuedInputMode = "follow_up",
) -> int:
    try:
        runtime = build_default_runtime(
            home,
            provider_options=provider_options,
            system_prompt=system_prompt,
        )
        async with build_default_mcp_runtime(runtime) as mcp:
            _print_mcp_startup_failures(mcp)
            result = await runtime.run(
                prompt,
                session_id=session_id,
                title=title,
                queue_mode=queue_mode,
            )
    except OuroError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return error_exit_code(exc)
    if result.run.status == "completed":
        _print_run_events(result.events)
        return 0
    if result.run.status == "failed":
        print(f"Run failed: {result.run.error or 'unknown error'}", file=sys.stderr)
        return _run_exit_code(result)
    print(f"Run stopped with status: {result.run.status}", file=sys.stderr)
    return _run_exit_code(result)


async def run_json(
    prompt: str,
    *,
    home: str | Path | None = None,
    session_id: str | None = None,
    title: str | None = None,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
    queue_mode: QueuedInputMode = "follow_up",
) -> int:
    try:
        runtime = build_default_runtime(
            home,
            provider_options=provider_options,
            system_prompt=system_prompt,
        )
        async with build_default_mcp_runtime(runtime) as mcp:
            _print_mcp_startup_failures(mcp)
            result = await runtime.run(
                prompt,
                session_id=session_id,
                title=title,
                queue_mode=queue_mode,
            )
    except OuroError as exc:
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
        return error_exit_code(exc)
    print(
        json.dumps(
            {
                "session_id": result.session.session_id,
                "run_id": result.run.run_id,
                "status": result.run.status,
                "final_text": result.final_text,
                "error": result.run.error,
                "usage": result.run.usage,
                "events": [event.model_dump(mode="json") for event in result.events],
            },
            ensure_ascii=False,
        )
    )
    return _run_exit_code(result)


def _run_exit_code(result: RunResult) -> int:
    if result.run.status == "completed":
        return 0
    if result.run.status in {"waiting_approval", "waiting_user", "paused"}:
        return 4
    if result.run.status == "aborted":
        return 5
    if result.run.status == "failed" and any(item.is_error for item in result.tool_results):
        return 6
    return 1


def _print_mcp_startup_failures(resource: MCPRuntimeResource) -> None:
    for record in resource.startup_failures:
        print(
            f"Warning: MCP server {record.config.server_id} failed to start: "
            f"{record.error or 'unknown error'}",
            file=sys.stderr,
        )


def _print_run_events(events: list[Any]) -> None:
    """Print text and reasoning deltas, matching interactive-mode formatting."""

    in_reasoning = False
    streamed_text = False

    for event in events:
        if event.event_type == "model_text_delta":
            text = event.payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            if in_reasoning:
                sys.stdout.write(f"{_RESET}\n")
                in_reasoning = False
            streamed_text = True
            sys.stdout.write(text)
        elif event.event_type == "model_reasoning_delta":
            reasoning = event.payload.get("reasoning_content")
            if not isinstance(reasoning, str) or not reasoning:
                continue
            if streamed_text:
                sys.stdout.write("\n")
                streamed_text = False
            if not in_reasoning:
                sys.stdout.write(_DIM_ITALIC)
                in_reasoning = True
            sys.stdout.write(reasoning)

    if in_reasoning:
        sys.stdout.write(_RESET)
    sys.stdout.write("\n")
    sys.stdout.flush()
