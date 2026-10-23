from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ouro.application.contracts.base import ApplicationStartupReport
from ouro.application.events import RuntimeEventSink
from ouro.application.inputs.queue import QueuedInputMode
from ouro.application.runtime.configuration import RuntimeProviderOptions
from ouro.application.runtime.service import RuntimeService
from ouro.cli.errors import error_exit_code
from ouro.core.approvals import SessionApprovalMode
from ouro.core.errors import OuroError
from ouro.core.interactions import SessionInteractionMode
from ouro.core.results import RunResult
from ouro.deployment import SessionBackend, TaskBackend
from ouro.local_application import (
    LocalApplication,
    build_local_mcp_runtime,
    build_local_runtime,
)
from ouro.mcp import MCPRuntimeResource

_DIM_ITALIC = "\x1b[2;3m"
_RESET = "\x1b[0m"


def build_default_runtime(
    home: str | Path | None = None,
    *,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
    event_sink: RuntimeEventSink | None = None,
    session_backend: SessionBackend | None = None,
) -> RuntimeService:
    return build_local_runtime(
        home,
        provider_options=provider_options,
        system_prompt=system_prompt,
        event_sink=event_sink,
        session_backend=session_backend,
    )


def build_default_mcp_runtime(
    runtime: RuntimeService,
    *,
    home: str | Path | None = None,
) -> MCPRuntimeResource:
    """Load configured MCP servers into the registry used by this runtime."""

    return build_local_mcp_runtime(runtime, home=home)


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
    interaction_mode: SessionInteractionMode | None = None,
    resource_refs: list[str] | None = None,
    session_backend: SessionBackend | None = None,
    task_backend: TaskBackend | None = None,
) -> int:
    try:
        runtime = build_default_runtime(
            home,
            provider_options=provider_options,
            system_prompt=system_prompt,
            session_backend=session_backend,
        )
        async with LocalApplication.from_runtime(
            runtime, home=home, task_backend=task_backend
        ) as application:
            _print_startup_diagnostics(application.startup_report)
            result = await runtime.run(
                prompt,
                session_id=session_id,
                title=title,
                queue_mode=queue_mode,
                approval_mode=approval_mode,
                interaction_mode=interaction_mode,
                resource_refs=resource_refs,
            )
    except OuroError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return error_exit_code(exc)
    for event in result.events:
        if event.event_type == "diagnostic":
            message = event.payload.get("message") or event.payload.get("error")
            if isinstance(message, str) and message:
                severity = event.payload.get("severity", "error")
                print(f"[{severity}] {message}", file=sys.stderr)
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
    interaction_mode: SessionInteractionMode | None = None,
    resource_refs: list[str] | None = None,
    session_backend: SessionBackend | None = None,
    task_backend: TaskBackend | None = None,
) -> int:
    startup_report = ApplicationStartupReport()
    try:
        runtime = build_default_runtime(
            home,
            provider_options=provider_options,
            system_prompt=system_prompt,
            session_backend=session_backend,
        )
        async with LocalApplication.from_runtime(
            runtime, home=home, task_backend=task_backend
        ) as application:
            startup_report = application.startup_report
            result = await runtime.run(
                prompt,
                session_id=session_id,
                title=title,
                queue_mode=queue_mode,
                approval_mode=approval_mode,
                interaction_mode=interaction_mode,
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
                "interaction_mode": result.session.interaction_mode,
                "final_text": result.final_text,
                "error": result.run.error,
                "usage": result.run.usage,
                "events": [event.model_dump(mode="json") for event in result.events],
                "startup_diagnostics": [
                    item.model_dump(mode="json", exclude_none=True)
                    for item in startup_report.diagnostics
                ],
            },
            ensure_ascii=False,
        )
    )
    return _run_exit_code(result)


def _run_exit_code(result: RunResult) -> int:
    if result.run.status == "completed":
        return 0
    if result.run.status in {"waiting_approval", "waiting_user", "waiting_child", "paused"}:
        return 4
    if result.run.status == "aborted":
        return 5
    if result.run.status == "failed" and any(item.is_error for item in result.tool_results):
        return 6
    return 1


def _print_startup_diagnostics(report: ApplicationStartupReport) -> None:
    for diagnostic in report.diagnostics:
        print(f"Warning: {diagnostic.message}", file=sys.stderr)
        if diagnostic.suggested_action:
            print(f"  Action: {diagnostic.suggested_action}", file=sys.stderr)


def _print_run_events(events: list[Any]) -> None:
    """Print text deltas and render reasoning deltas in italics."""

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
