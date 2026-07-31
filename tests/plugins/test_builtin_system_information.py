from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.builtin_plugins import LOCAL_BUILTIN_PLUGINS
from ouro.builtin_plugins.system_information import SYSTEM_INFORMATION_PLUGIN
from ouro.builtin_plugins.system_information.system_information_plugin import (
    RuntimeFacts,
    SystemInformationPlugin,
)
from ouro.cli.interactive_commands import handle_command
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.sdk.plugins import v1 as sdk
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry

PLUGIN_ID = "dev.ouro.system_information"


def test_system_information_manifest_is_default_enabled_context_contract() -> None:
    manifest = SYSTEM_INFORMATION_PLUGIN.manifest

    assert manifest.plugin_id == PLUGIN_ID
    assert [item.id for item in manifest.contributes.context_providers] == ["runtime-facts"]
    assert manifest.contributes.context_providers[0].priority == 10_000
    assert manifest.contributes.context_providers[0].failure_mode == "omit"
    assert manifest.permissions == []
    assert SYSTEM_INFORMATION_PLUGIN.default_enabled is True
    assert LOCAL_BUILTIN_PLUGINS.require(PLUGIN_ID) is SYSTEM_INFORMATION_PLUGIN


def test_system_information_is_tagged_prompt_tail_with_day_precision() -> None:
    plugin = SystemInformationPlugin(
        facts=RuntimeFacts(
            platform="Test OS <1>",
            architecture="test64",
            runtime="OURO test; CPython test",
            shell="/bin/test & tools",
            preferred_encoding="UTF-8",
            path_style="POSIX",
        ),
        clock=lambda: datetime(
            2026,
            7,
            31,
            23,
            59,
            58,
            tzinfo=timezone(timedelta(hours=8), name="CST"),
        ),
        fallback_working_directory="/fallback",
    )

    output = plugin.system_information(
        sdk.ContextCall(
            session_id="ses_1",
            run_id="run_1",
            message_count=1,
            model_round=1,
            workspace="/work & project",
        )
    )

    assert len(output.fragments) == 1
    fragment = output.fragments[0]
    assert isinstance(fragment, sdk.ContextFragment)
    assert fragment.target == "system_prompt"
    assert fragment.persistence == "ephemeral"
    assert fragment.semantic_tag == "ouro-system-information"
    assert fragment.text == (
        "System platform: Test OS <1>\n"
        "Architecture: test64\n"
        "Runtime environment: OURO test; CPython test\n"
        "Current date: 2026-07-31\n"
        "Time zone: CST (UTC+08:00)\n"
        "Workspace: /work & project\n"
        "Shell: /bin/test & tools\n"
        "Path style: POSIX\n"
        "Preferred encoding: UTF-8"
    )
    assert "23:59" not in fragment.text


def test_system_information_uses_process_directory_when_session_has_no_workspace() -> None:
    plugin = SystemInformationPlugin(
        facts=RuntimeFacts(
            platform="Test OS",
            architecture="test64",
            runtime="OURO test",
            shell="test",
            preferred_encoding="UTF-8",
            path_style="POSIX",
        ),
        clock=lambda: datetime(2026, 7, 31, tzinfo=UTC),
        fallback_working_directory="/fallback",
    )

    output = plugin.system_information(
        sdk.ContextCall(
            session_id="ses_1",
            run_id="run_1",
            message_count=0,
            model_round=1,
        )
    )
    fragment = output.fragments[0]
    assert isinstance(fragment, sdk.ContextFragment)
    assert "Workspace: /fallback" in fragment.text


@pytest.mark.asyncio
async def test_default_builtin_injects_runtime_prompt_without_persisting_message(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = FauxProvider(
        [
            ProviderStreamEvent(type="text_delta", text="ok"),
            ProviderStreamEvent(type="done"),
        ]
    )
    system_prompts = []
    original_stream = provider.stream

    async def recording_stream(**kwargs):
        system_prompts.append(kwargs["system_prompt"])
        async for event in original_stream(**kwargs):
            yield event

    provider.stream = recording_stream  # type: ignore[method-assign]
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
        default_workspace=tmp_path / "workspace",
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )
    application = LocalApplication.from_runtime(runtime, home=tmp_path)

    async with application:
        assert application.plugins is not None
        assert application.plugins.show(PLUGIN_ID).status == "active"
        result = await runtime.run("Inspect the environment")
        context_command = await handle_command(
            "/context full",
            runtime,
            result.session.session_id,
        )

    assert system_prompts[0].endswith("</ouro-system-information>")
    assert f"Workspace: {tmp_path / 'workspace'}" in system_prompts[0]
    snapshot = store.load_runtime_session(result.session.session_id)
    assert not any(
        message.metadata.get("kind") == "context_provider" for message in snapshot.messages
    )
    assert context_command.output.startswith("Context: ctx_")
    assert "<ouro-system-information>" in context_command.output
    assert context_command.output.index("<ouro-system-information>") < (
        context_command.output.index("</ouro-system-information>")
    )
