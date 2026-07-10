from __future__ import annotations

import json
import sys
from pathlib import Path

from ouro.application.events import RuntimeEventSink
from ouro.application.runtime import RuntimeService
from ouro.cli.errors import error_exit_code
from ouro.config import PromptConfigManager, ProviderConfigManager, ProviderRuntimeOptions
from ouro.config.credentials import CredentialRef, CredentialResolver
from ouro.core.adapter import RuntimeAdapter
from ouro.core.context import ContextBuilder
from ouro.core.errors import ConfigError, OuroError
from ouro.prompts import render_agent_skills_prompt
from ouro.providers import FauxProvider, ModelMetadata, OpenAICompatibleProvider
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
    adapter = _build_adapter(resolved_options)
    skills_prompt = render_agent_skills_prompt(discover_available_skills(home=store.root))
    context_builder = ContextBuilder(base_context=[skills_prompt] if skills_prompt else [])
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


def _build_adapter(options: RuntimeProviderOptions) -> RuntimeAdapter:
    provider_kind = options.provider or "faux"
    timeout = options.timeout or 60.0
    if provider_kind == "faux":
        model_id = options.model or "faux"
        provider_id = options.provider_id or "faux"
        return RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id=model_id, provider_id=provider_id, display_name="Faux"),
        )

    if provider_kind == "openai-compatible":
        if not options.base_url:
            raise ConfigError("--base-url is required for openai-compatible provider")
        if not options.model:
            raise ConfigError("--model is required for openai-compatible provider")
        provider_id = options.provider_id or "openai_compatible"
        credential_ref = _credential_ref(options)
        api_key = CredentialResolver().resolve(credential_ref)
        provider = OpenAICompatibleProvider(
            base_url=options.base_url,
            api_key=api_key,
            provider_id=provider_id,
            timeout=timeout,
            request_options=options.extra,
        )
        return RuntimeAdapter(
            provider,
            ModelMetadata(
                model_id=options.model,
                provider_id=provider_id,
                display_name=options.model,
            ),
        )

    raise ConfigError(f"unsupported provider: {provider_kind}")


def _credential_ref(options: RuntimeProviderOptions) -> CredentialRef:
    if options.credential_ref is not None:
        return options.credential_ref
    if options.api_key_env:
        return CredentialRef.parse(f"env:{options.api_key_env}")
    return CredentialRef.parse(options.api_key)
