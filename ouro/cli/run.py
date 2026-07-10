from __future__ import annotations

import json
import sys
from pathlib import Path

from ouro.application.events import RuntimeEventSink
from ouro.application.runtime import RuntimeService
from ouro.cli.errors import error_exit_code
from ouro.config import PromptConfigManager, ProviderConfigManager, ProviderRuntimeOptions
from ouro.core.context import ContextBuilder
from ouro.core.errors import OuroError
from ouro.prompts import render_agent_skills_prompt
from ouro.providers import build_runtime_adapter
from ouro.sessions import LocalSessionStore
from ouro.skills import discover_available_skills
from ouro.tools import ToolRegistry, builtin_tools

RuntimeProviderOptions = ProviderRuntimeOptions


def build_default_runtime(
    home: str | Path | None = None,
    *,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
    event_sink: RuntimeEventSink | None = None,
) -> RuntimeService:
    store = LocalSessionStore(home)
    store.initialize()
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    resolved_options = ProviderConfigManager(store.root).resolve_runtime_options(
        provider_options or RuntimeProviderOptions(),
    )
    adapter = build_runtime_adapter(resolved_options)
    skills_prompt = render_agent_skills_prompt(discover_available_skills(home=store.root))
    context_builder = ContextBuilder(skill_instructions=skills_prompt)
    resolved_system_prompt = PromptConfigManager(store.root).resolve_system_prompt(system_prompt)
    return RuntimeService(
        store=store,
        adapter=adapter,
        tools=registry,
        context_builder=context_builder,
        system_prompt=resolved_system_prompt,
        event_sink=event_sink,
    )


async def run_print(
    prompt: str,
    *,
    home: str | Path | None = None,
    session_id: str | None = None,
    title: str | None = None,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
) -> int:
    try:
        runtime = build_default_runtime(
            home,
            provider_options=provider_options,
            system_prompt=system_prompt,
        )
        result = await runtime.run(prompt, session_id=session_id, title=title)
    except OuroError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return error_exit_code(exc)
    if result.run.status == "completed":
        print(result.final_text)
        return 0
    if result.run.status == "failed":
        print(f"Run failed: {result.run.error or 'unknown error'}", file=sys.stderr)
        return 1
    print(f"Run stopped with status: {result.run.status}", file=sys.stderr)
    return 4 if result.run.status in {"waiting_approval", "waiting_user"} else 1


async def run_json(
    prompt: str,
    *,
    home: str | Path | None = None,
    session_id: str | None = None,
    title: str | None = None,
    provider_options: RuntimeProviderOptions | None = None,
    system_prompt: str | None = None,
) -> int:
    try:
        runtime = build_default_runtime(
            home,
            provider_options=provider_options,
            system_prompt=system_prompt,
        )
        result = await runtime.run(prompt, session_id=session_id, title=title)
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
    return 0 if result.run.status == "completed" else 1
