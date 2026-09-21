import asyncio
from threading import get_ident

import pytest

from ouro.application.configuration import ConfigurationFacade
from ouro.application.runtime import RuntimeService
from ouro.application.runtime_configuration import RuntimeConfigurationResolver
from ouro.application.sessions import SessionService
from ouro.config.prompts import PromptConfigManager, PromptDraft
from ouro.core.adapter import RuntimeAdapter
from ouro.core.context_providers import (
    ContextProviderRegistration,
    ContextProviderRegistry,
    ContextProviderResult,
)
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta
from ouro.local_application import LocalApplication, build_local_runtime
from ouro.prompts import DEFAULT_SYSTEM_PROMPT
from ouro.providers import FauxProvider
from ouro.sessions import LocalSessionStore
from ouro.tools import ToolRegistry, builtin_tools


class RecordingProvider(FauxProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.prompts = []

    async def stream(self, *, system_prompt="", **kwargs):
        self.prompts.append(system_prompt)
        async for event in super().stream(system_prompt=system_prompt, **kwargs):
            yield event


def _runtime(tmp_path, provider, **kwargs):
    store = LocalSessionStore(tmp_path)
    store.initialize()
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="test", provider_id="faux")),
        tools=ToolRegistry(),
        prompt_context_resolver=RuntimeConfigurationResolver(tmp_path).prompt_context,
        **kwargs,
    )


@pytest.mark.parametrize("override", [None, "CLI base", ""])
async def test_personal_prompts_reach_prepared_requests_in_order(tmp_path, override):
    facade = ConfigurationFacade(tmp_path)
    entries = (
        ("replacement", PromptDraft(text="Personal base\n", enabled=True)),
        (
            "addition",
            PromptDraft(text="First\n\n", enabled=True, description="DISPLAY-ONLY-ANNOTATION"),
        ),
        ("addition", PromptDraft(text="DISABLED-CONTENT")),
        ("addition", PromptDraft(text="Second", enabled=True)),
    )
    for revision, (kind, entry) in enumerate(entries):
        facade.save_prompt(kind, entry, expected_revision=revision)
    runtime = build_local_runtime(tmp_path, system_prompt=override)
    base = "Personal base\n\n" if override is None else f"{override}\n\n" if override else ""
    expected = base + "First\n\nSecond"
    assert runtime.context_preparation.system_prompt == expected
    if override is None:
        assert facade.system_prompt().effective_system_prompt == expected
    async with LocalApplication.from_runtime(runtime):
        result = await runtime.run("hello", workspace=tmp_path)
        snapshot = SessionService(runtime.store).context(result.session.session_id)
        assert snapshot is not None
        prompt = snapshot.provider_context.system_prompt
        assert prompt.startswith(expected + "\n\n")
        assert "<ouro-agent-instructions>" in prompt
        assert "<ouro-system-information>" in prompt
        assert "DISPLAY-ONLY-ANNOTATION" not in prompt
        assert "DISABLED-CONTENT" not in prompt
        # A separate configuration client updates the already running session.
        facade.set_prompt_enabled(
            "replacement",
            facade.system_prompt().config.replacements[0].id,
            False,
            expected_revision=4,
        )
        result = await runtime.run("again", session_id=result.session.session_id)
        snapshot = SessionService(runtime.store).context(result.session.session_id)
        assert snapshot is not None
        base = DEFAULT_SYSTEM_PROMPT if override is None else override
        expected = (base.rstrip("\n") + "\n\n" if base else "") + "First\n\nSecond"
        assert snapshot.provider_context.system_prompt.startswith(expected + "\n\n")
        record = runtime.store.load_latest_context(result.session.session_id)
        assert record is not None
        assert record.context_metadata["personal_prompt"] == {
            "revision": 5,
            "source": "default" if override is None else "override",
            "replacement_id": None,
            "addition_ids": [
                entry.id for entry in facade.system_prompt().config.additions if entry.enabled
            ],
        }

        additions = facade.system_prompt().config.additions
        facade.save_prompt(
            "addition",
            PromptDraft(text="Edited", enabled=True),
            entry_id=additions[0].id,
            expected_revision=5,
        )
        facade.move_prompt("addition", additions[2].id, 1, expected_revision=6)
        result = await runtime.run("edited", session_id=result.session.session_id)
        record = runtime.store.load_latest_context(result.session.session_id)
        assert record is not None
        assert "Second\n\nEdited" in record.system_prompt
        assert "First" not in record.system_prompt
        assert record.context_metadata["personal_prompt"]["revision"] == 7


async def test_concurrent_preparations_keep_their_own_prompt_snapshot(tmp_path):
    manager = PromptConfigManager(tmp_path)
    manager.save_entry("replacement", PromptDraft(text="Before", enabled=True), expected_revision=0)
    first_preparing = asyncio.Event()
    release = asyncio.Event()

    async def gated_context(call):
        if not first_preparing.is_set():
            first_preparing.set()
            await release.wait()
        return ContextProviderResult()

    contexts = ContextProviderRegistry()
    contexts.replace_owner(
        "test",
        (
            ContextProviderRegistration(
                plugin_id="test",
                provider_id="gate",
                handler=gated_context,
            ),
        ),
    )
    provider = RecordingProvider()
    runtime = _runtime(tmp_path, provider, context_providers=contexts)
    async with runtime:
        first_task = asyncio.create_task(runtime.run("first"))
        try:
            await asyncio.wait_for(first_preparing.wait(), 5)
            manager.save_entry(
                "replacement",
                PromptDraft(text="After", enabled=True),
                expected_revision=1,
            )
            second = await runtime.run("second")
        finally:
            release.set()
            first = await first_task
        assert first.run.status == second.run.status == "completed"
        assert provider.prompts == ["After", "Before"]
        first_snapshot = runtime.store.load_latest_context(first.session.session_id)
        second_snapshot = runtime.store.load_latest_context(second.session.session_id)
        assert first_snapshot.context_metadata["personal_prompt"]["revision"] == 1
        assert second_snapshot.context_metadata["personal_prompt"]["revision"] == 2


async def test_stream_is_unchanged_but_tool_continuation_uses_new_prompt(tmp_path):
    manager = PromptConfigManager(tmp_path)
    manager.save_entry("replacement", PromptDraft(text="Before", enabled=True), expected_revision=0)
    streaming = asyncio.Event()
    release = asyncio.Event()

    class GatedProvider(RecordingProvider):
        async def stream(self, **kwargs):
            async for event in super().stream(**kwargs):
                yield event
                if len(self.prompts) == 1 and event.type == "text_delta":
                    streaming.set()
                    await release.wait()

    provider = GatedProvider(
        event_batches=[
            [
                ProviderStreamEvent(type="text_delta", text="Reading"),
                ProviderStreamEvent(
                    type="tool_call_delta",
                    tool_call_delta=ToolCallDelta(
                        index=0,
                        id_delta="call_read",
                        name_delta="Read",
                        arguments_delta='{"path":"sample.txt"}',
                    ),
                ),
                ProviderStreamEvent(type="done"),
            ],
            [ProviderStreamEvent(type="text_delta", text="Done"), ProviderStreamEvent(type="done")],
        ]
    )
    runtime = _runtime(tmp_path, provider)
    for tool in builtin_tools():
        runtime.tools.register(tool)
    (tmp_path / "sample.txt").write_text("sample", encoding="utf-8")
    async with runtime:
        task = asyncio.create_task(runtime.run("read sample.txt", workspace=tmp_path))
        try:
            await asyncio.wait_for(streaming.wait(), 5)
            manager.save_entry(
                "replacement",
                PromptDraft(text="After", enabled=True),
                expected_revision=1,
            )
            assert provider.prompts == ["Before"]
        finally:
            release.set()
            result = await task
        assert result.run.status == "completed"
        assert len(result.tool_results) == 1
        assert not result.tool_results[0].is_error
        assert provider.prompts == ["Before", "After"]
        snapshot = runtime.store.load_latest_context(result.session.session_id)
        assert snapshot.model_round == 2
        assert snapshot.context_metadata["personal_prompt"]["revision"] == 2


@pytest.mark.parametrize(
    "invalid",
    [
        "{broken-json",
        '{"prompts":{"replacements":[{"id":"bad","text":"PRIVATE-BODY","enabled":"yes"}]}}',
    ],
)
async def test_invalid_live_config_blocks_send_and_can_be_corrected_and_resumed(tmp_path, invalid):
    manager = PromptConfigManager(tmp_path)
    manager.save_entry("replacement", PromptDraft(text="Before", enabled=True), expected_revision=0)
    provider = RecordingProvider()
    runtime = _runtime(tmp_path, provider)
    async with runtime:
        first = await runtime.run("first")
        saved = manager.config_path.read_bytes()
        manager.config_path.write_text(invalid, encoding="utf-8")
        blocked = await runtime.run("blocked", session_id=first.session.session_id)
        assert blocked.run.status == "paused"
        assert provider.prompts == ["Before"]
        diagnostic = next(event for event in blocked.events if event.event_type == "diagnostic")
        assert diagnostic.payload["code"] == "prompt_configuration_invalid"
        assert "PRIVATE-BODY" not in diagnostic.payload["message"]
        manager.config_path.write_bytes(saved)
        manager.save_entry(
            "replacement",
            PromptDraft(text="Corrected", enabled=True),
            expected_revision=1,
        )
        resumed = await runtime.continue_run(session_id=first.session.session_id)
        assert resumed.run.status == "completed"
        assert resumed.run.run_id == blocked.run.run_id
        assert provider.prompts == ["Before", "Corrected"]


async def test_prompt_resolution_runs_off_the_event_loop(tmp_path):
    runtime = _runtime(tmp_path, RecordingProvider())
    resolver = runtime.context_preparation.prompt_context_resolver
    threads = []
    main_thread = get_ident()

    def recording_resolver():
        threads.append(get_ident())
        return resolver()

    runtime.context_preparation.prompt_context_resolver = recording_resolver
    async with runtime:
        result = await runtime.run("hello")
        assert result.run.status == "completed"
    assert len(threads) == 1
    assert threads[0] != main_thread
