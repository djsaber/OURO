from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ouro.application.events import RuntimeEventSink
from ouro.application.queued_inputs import QueuedInputMode
from ouro.application.resources import ResourceService
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_configuration import (
    RuntimeConfigurationResolver,
    RuntimeProviderOptions,
)
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.cli.errors import error_exit_code
from ouro.config import CompactionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.approvals import SessionApprovalMode
from ouro.core.context import ContextBuilder
from ouro.core.errors import OuroError
from ouro.core.results import RunResult
from ouro.mcp import MCPRuntimeResource
from ouro.providers import build_runtime_adapter
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry, builtin_tools

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
    usage_ledger = LocalUsageLedger(store.root)
    usage_ledger.initialize()
    usage_service = UsageService(usage_ledger)
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    configuration = RuntimeConfigurationResolver(store.root)
    resolved = configuration.snapshot(
        provider_options=provider_options,
        system_prompt=system_prompt,
    )
    adapter = build_runtime_adapter(resolved.provider_options)
    model_catalog = configuration.model_catalog(
        adapter,
        adapter_builder=build_runtime_adapter,
    )
    context_builder = ContextBuilder(skill_instructions=resolved.skills_prompt)
    compaction_adapter, compaction_adapter_error = _configured_compaction_adapter(
        configuration,
        resolved.compaction,
    )
    return RuntimeService(
        store=store,
        adapter=adapter,
        model_catalog=model_catalog,
        tools=registry,
        context_builder=context_builder,
        system_prompt=resolved.system_prompt,
        event_sink=event_sink,
        permission_overrides=resolved.permission_overrides,
        tool_execution_config=resolved.tool_execution,
        run_budget=resolved.run_budget,
        compaction_config=resolved.compaction,
        compaction_adapter=compaction_adapter,
        compaction_adapter_error=compaction_adapter_error,
        queued_input_config=resolved.queued_inputs,
        resources=resources,
        usage_service=usage_service,
        default_workspace=Path.cwd(),
    )


def build_default_mcp_runtime(runtime: RuntimeService) -> MCPRuntimeResource:
    """Load configured MCP servers into the registry used by this runtime."""

    return MCPRuntimeResource.from_config(runtime.store.root, runtime.tools)


def _configured_compaction_adapter(
    configuration: RuntimeConfigurationResolver,
    config: CompactionConfig,
) -> tuple[RuntimeAdapter | None, str | None]:
    if not config.model.has_dedicated_endpoint:
        return None, None
    try:
        options = configuration.compaction_provider_options(config)
        assert options is not None
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
    approval_mode: SessionApprovalMode | None = None,
    resource_refs: list[str] | None = None,
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
                approval_mode=approval_mode,
                resource_refs=resource_refs,
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
    approval_mode: SessionApprovalMode | None = None,
    resource_refs: list[str] | None = None,
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
                approval_mode=approval_mode,
                resource_refs=resource_refs,
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
                "approval_mode": result.session.approval_mode,
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
