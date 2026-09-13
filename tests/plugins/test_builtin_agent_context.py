from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.builtin_plugins import LOCAL_BUILTIN_PLUGINS
from ouro.builtin_plugins.agent_context import AGENT_CONTEXT_PLUGIN
from ouro.builtin_plugins.agent_context.agent_context_plugin import (
    AgentContextPlugin,
    RuntimeFacts,
)
from ouro.builtin_plugins.agent_context.instructions import (
    AgentInstructionError,
    AgentInstructionLoader,
    render_agent_instructions,
)
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.local_application import LocalApplication
from ouro.providers import FauxProvider
from ouro.sdk.plugins import v2 as sdk
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry

PLUGIN_ID = "dev.ouro.agent_context"


def test_agent_context_manifest_is_default_enabled_context_contract() -> None:
    manifest = AGENT_CONTEXT_PLUGIN.manifest

    assert manifest.plugin_id == PLUGIN_ID
    assert manifest.name == "Agent Context"
    assert [item.id for item in manifest.contributes.context_providers] == [
        "agent-instructions",
        "runtime-facts",
    ]
    assert [item.priority for item in manifest.contributes.context_providers] == [9_000, 10_000]
    assert all(item.failure_mode == "omit" for item in manifest.contributes.context_providers)
    assert manifest.permissions == []
    assert AGENT_CONTEXT_PLUGIN.default_enabled is True
    assert LOCAL_BUILTIN_PLUGINS.require(PLUGIN_ID) is AGENT_CONTEXT_PLUGIN


def test_system_information_is_tagged_prompt_tail_with_day_precision() -> None:
    plugin = AgentContextPlugin(
        instructions=AgentInstructionLoader(ouro_home="/missing", user_home="/missing"),
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

    output = plugin.system_information(_context_call(workspace="/work & project"))

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
    plugin = AgentContextPlugin(
        instructions=AgentInstructionLoader(ouro_home="/missing", user_home="/missing"),
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

    output = plugin.system_information(_context_call())

    fragment = output.fragments[0]
    assert isinstance(fragment, sdk.ContextFragment)
    assert "Workspace: /fallback" in fragment.text


def test_instruction_loader_merges_user_and_project_documents_from_broad_to_specific(
    tmp_path: Path,
) -> None:
    ouro_home = tmp_path / "ouro-home"
    user_home = tmp_path / "profile"
    project = tmp_path / "project"
    workspace = project / "packages" / "api"
    (project / ".git").mkdir(parents=True)
    workspace.mkdir(parents=True)
    documents = (
        (user_home / ".agents" / "AGENTS.md", "shared user"),
        (ouro_home / "AGENTS.md", "ouro user"),
        (project / "AGENTS.md", "project root"),
        (project / "packages" / "AGENTS.md", "package"),
        (workspace / "AGENTS.md", "workspace"),
    )
    for path, content in documents:
        _write(path, content)

    loaded = AgentInstructionLoader(
        ouro_home=ouro_home,
        user_home=user_home,
    ).load(workspace)

    assert [document.content for document in loaded] == [
        "shared user",
        "ouro user",
        "project root",
        "package",
        "workspace",
    ]
    assert [document.scope for document in loaded] == [
        "user",
        "user",
        "project",
        "project",
        "project",
    ]
    rendered = render_agent_instructions(loaded)
    positions = [rendered.index(f"\n\n{content}") for _, content in documents]
    assert positions == sorted(positions)
    assert "later and more specific source takes precedence" in rendered


def test_instruction_loader_uses_workspace_as_boundary_without_git_repository(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    workspace = parent / "workspace"
    workspace.mkdir(parents=True)
    _write(parent / "AGENTS.md", "outside")
    _write(workspace / "AGENTS.md", "inside")

    loaded = AgentInstructionLoader(
        ouro_home=tmp_path / "home",
        user_home=tmp_path / "profile",
    ).load(workspace)

    assert [document.content for document in loaded] == ["inside"]


def test_instruction_loader_ignores_product_specific_and_memory_documents(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write(workspace / "CLAUDE.md", "claude")
    _write(workspace / "GEMINI.md", "gemini")
    _write(workspace / "MEMORY.md", "memory")
    _write(workspace / "SKILL.md", "skill")

    loaded = AgentInstructionLoader(
        ouro_home=tmp_path / "home",
        user_home=tmp_path / "profile",
    ).load(workspace)

    assert loaded == ()


def test_instruction_loader_reloads_documents_for_each_request(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    document = workspace / "AGENTS.md"
    _write(document, "version one")
    loader = AgentInstructionLoader(
        ouro_home=tmp_path / "home",
        user_home=tmp_path / "profile",
    )

    first = loader.load(workspace)
    document.write_text("version two", encoding="utf-8")
    second = loader.load(workspace)

    assert first[0].content == "version one"
    assert second[0].content == "version two"


def test_default_ouro_home_uses_the_real_profile_for_shared_instructions() -> None:
    loader = AgentInstructionLoader(ouro_home=Path.home() / ".ouro")

    assert loader.user_home == Path.home().resolve()


def test_instruction_loader_rejects_invalid_encoding_and_combined_size(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    document = workspace / "AGENTS.md"
    document.write_bytes(b"\xff")
    loader = AgentInstructionLoader(
        ouro_home=tmp_path / "home",
        user_home=tmp_path / "profile",
        max_bytes=8,
    )

    with pytest.raises(AgentInstructionError, match="must be UTF-8"):
        loader.load(workspace)

    document.write_text("nine-byte!", encoding="utf-8")
    with pytest.raises(AgentInstructionError, match="exceeds 8 bytes"):
        loader.load(workspace)


def test_agent_instructions_keep_nested_discovery_contract_without_current_documents(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plugin = AgentContextPlugin(
        instructions=AgentInstructionLoader(
            ouro_home=tmp_path / "home",
            user_home=tmp_path / "profile",
        )
    )

    output = plugin.agent_instructions(_context_call(workspace=str(workspace)))

    assert len(output.fragments) == 1
    fragment = output.fragments[0]
    assert isinstance(fragment, sdk.ContextFragment)
    assert fragment.semantic_tag == "ouro-agent-instructions"
    assert "check for a more specific AGENTS.md" in fragment.text


@pytest.mark.asyncio
async def test_default_builtin_injects_instructions_then_runtime_facts_without_persistence(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    _write(home / "AGENTS.md", "Always validate the final result.")
    _write(workspace / "AGENTS.md", "Run the workspace tests.")
    store = LocalSessionStore(home)
    store.initialize()
    provider = FauxProvider(
        [
            ProviderStreamEvent(type="text_delta", text="ok"),
            ProviderStreamEvent(type="done"),
        ]
    )
    system_prompts: list[str] = []
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
        default_workspace=workspace,
        environment=RuntimeEnvironment.local(home, store.sessions_dir),
    )
    application = LocalApplication.from_runtime(runtime, home=home)

    async with application:
        assert application.plugins is not None
        assert application.plugins.show(PLUGIN_ID).status == "active"
        result = await runtime.run("Inspect the environment")
        context = runtime.sessions.context(result.session.session_id)

    prompt = system_prompts[0]
    assert "Always validate the final result." in prompt
    assert "Run the workspace tests." in prompt
    assert prompt.index("<ouro-agent-instructions>") < prompt.index("<ouro-system-information>")
    assert prompt.endswith("</ouro-system-information>")
    assert f"Workspace: {workspace}" in prompt
    snapshot = store.load_runtime_session(result.session.session_id)
    assert not any(
        message.metadata.get("kind") == "context_provider" for message in snapshot.messages
    )
    assert context is not None
    assert "<ouro-agent-instructions>" in context.provider_context.system_prompt
    assert "<ouro-system-information>" in context.provider_context.system_prompt


def _context_call(*, workspace: str | None = None) -> sdk.ContextCall:
    return sdk.ContextCall(
        session_id="ses_1",
        run_id="run_1",
        message_count=1,
        model_round=1,
        workspace=workspace,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
