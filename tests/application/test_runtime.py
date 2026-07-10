import asyncio
import sys

import pytest

from ouro.application.runtime import RuntimeService
from ouro.core.adapter import RuntimeAdapter
from ouro.core.context import ContextBuilder
from ouro.core.hooks import HookRegistration, HookRegistry, HookResult
from ouro.core.messages import MessageRecord
from ouro.prompts import DEFAULT_SYSTEM_PROMPT, render_agent_skills_prompt
from ouro.providers import (
    FauxProvider,
    ModelMetadata,
    ProviderCheckResult,
    ProviderStreamEvent,
    Usage,
)
from ouro.sessions import LocalSessionStore
from ouro.skills import SkillMetadata
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    builtin_tools,
)


def _tools() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    return registry


class WaitingProvider:
    provider_id = "waiting"

    def __init__(self) -> None:
        self.closed = False

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages,
        tools=None,
    ):
        del model, system_prompt, messages, tools
        try:
            yield ProviderStreamEvent(type="text_delta", text="begin")
            while True:
                await asyncio.sleep(1)
        finally:
            self.closed = True

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class GateProvider:
    provider_id = "gate"

    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.calls = 0

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages,
        tools=None,
    ):
        del model, system_prompt, messages, tools
        self.calls += 1
        if self.calls == 1:
            yield ProviderStreamEvent(type="text_delta", text="first")
            self.first_started.set()
            await self.release_first.wait()
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="second")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class ExplodingProvider:
    provider_id = "explode"

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages,
        tools=None,
    ):
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(type="text_delta", text="partial")
        raise RuntimeError("stream broke")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class CapturingProvider:
    provider_id = "capture"

    def __init__(self) -> None:
        self.messages: list[MessageRecord] = []
        self.system_prompt = ""
        self.calls = 0

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages,
        tools=None,
    ):
        del model, tools
        self.calls += 1
        self.system_prompt = system_prompt
        self.messages = list(messages)
        yield ProviderStreamEvent(type="text_delta", text="ok")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


async def _wait_for_active_session(store: LocalSessionStore):
    for _ in range(100):
        sessions = store.list_sessions()
        if sessions and sessions[0].last_run_id:
            return sessions[0]
        await asyncio.sleep(0.01)
    raise AssertionError("active session was not created")


async def _wait_for_event(store: LocalSessionStore, session_id: str, event_type: str) -> None:
    for _ in range(100):
        snapshot = store.load_session(session_id, recover=False)
        if any(event.event_type == event_type for event in snapshot.events):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"event was not emitted: {event_type}")


async def _wait_for_checkpoint(
    store: LocalSessionStore,
    session_id: str,
    expected_partial: str,
) -> None:
    for _ in range(100):
        snapshot = store.load_session(session_id, recover=False)
        if snapshot.runs:
            checkpoint = snapshot.runs[0].metadata.get("checkpoint")
            if (
                isinstance(checkpoint, dict)
                and checkpoint.get("partial_assistant_text") == expected_partial
            ):
                return
        await asyncio.sleep(0.01)
    raise AssertionError("run checkpoint was not persisted")


@pytest.mark.asyncio
async def test_runtime_injects_default_system_prompt(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="capture", provider_id="capture")),
        tools=_tools(),
    )

    await runtime.run("hello")

    assert provider.system_prompt == DEFAULT_SYSTEM_PROMPT
    assert provider.messages[0].role == "user"


@pytest.mark.asyncio
async def test_runtime_allows_system_prompt_override(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="capture", provider_id="capture")),
        tools=_tools(),
        system_prompt="Custom system prompt.",
    )

    await runtime.run("hello")

    assert provider.system_prompt == "Custom system prompt."
    assert provider.messages[0].role == "user"


@pytest.mark.asyncio
async def test_runtime_persists_text_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(type="text_delta", text="Hello"),
                    ProviderStreamEvent(type="text_delta", text=" world"),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("Say hi")
    snapshot = store.load_session(result.session.session_id)

    assert result.final_text == "Hello world"
    assert result.run.status == "completed"
    assert [message.role for message in snapshot.messages] == ["user", "assistant"]
    assert snapshot.messages[-1].text_content == "Hello world"
    assert "run_completed" in [event.event_type for event in snapshot.events]


@pytest.mark.asyncio
async def test_runtime_persists_partial_assistant_when_provider_stream_raises(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            ExplodingProvider(),
            ModelMetadata(model_id="explode", provider_id="explode"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("Break mid stream")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "failed"
    assert result.run.error == "stream broke"
    assert result.final_text == "partial"
    assert [message.role for message in snapshot.messages] == ["user", "assistant"]
    assert snapshot.messages[-1].text_content == "partial"
    assert snapshot.messages[-1].metadata["incomplete"] is True
    assert snapshot.session.message_count == 2
    assert snapshot.session.last_message_id == snapshot.messages[-1].message_id
    assert snapshot.events[-1].event_type == "run_failed"
    assert store.load_index()["sessions"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_runtime_hook_adds_context_to_provider(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = FauxProvider(
        [
            ProviderStreamEvent(type="text_delta", text="ok"),
            ProviderStreamEvent(type="done"),
        ]
    )
    seen_messages = []
    seen_system_prompts = []

    original_stream = provider.stream

    async def recording_stream(**kwargs):
        seen_messages.extend(kwargs["messages"])
        seen_system_prompts.append(kwargs["system_prompt"])
        async for event in original_stream(**kwargs):
            yield event

    provider.stream = recording_stream
    hooks = HookRegistry()
    hooks.register(
        HookRegistration(
            plugin_id="ctx",
            hook_name="after_context_build",
            source="official",
            handler=lambda context: HookResult(added_context=["extra context"]),
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="faux", provider_id="faux")),
        tools=_tools(),
        hooks=hooks,
    )

    await runtime.run("Use context")

    assert seen_system_prompts == [DEFAULT_SYSTEM_PROMPT]
    assert seen_messages[0].role == "system_synthetic"
    assert seen_messages[0].text_content == "extra context"


@pytest.mark.asyncio
async def test_runtime_merges_context_builder_skills_before_hook_context(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingProvider()
    skills_prompt = render_agent_skills_prompt(
        [
            SkillMetadata(
                name="pdf",
                description="Extract text from PDFs.",
                location=str(tmp_path / "skills" / "pdf" / "SKILL.md"),
            )
        ]
    )
    context_builder = ContextBuilder(skill_instructions=skills_prompt)
    hooks = HookRegistry()
    hooks.register(
        HookRegistration(
            plugin_id="ctx",
            hook_name="after_context_build",
            source="official",
            handler=lambda context: HookResult(added_context=["plugin context"]),
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
        hooks=hooks,
        context_builder=context_builder,
    )

    await runtime.run("Use context")

    assert provider.system_prompt.startswith(DEFAULT_SYSTEM_PROMPT.strip())
    assert "<skills_instructions>" in provider.system_prompt
    assert "<name>pdf</name>" in provider.system_prompt
    synthetic_messages = [
        message.text_content for message in provider.messages if message.role == "system_synthetic"
    ]
    assert synthetic_messages == ["plugin context"]


@pytest.mark.asyncio
async def test_runtime_fails_empty_model_completion(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider([ProviderStreamEvent(type="done")]),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("Say hi")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "failed"
    assert result.run.error == "provider returned an empty assistant message"
    assert not any(message.role == "assistant" for message in snapshot.messages)
    assert snapshot.events[-1].event_type == "run_failed"


@pytest.mark.asyncio
async def test_runtime_stops_for_tool_approval(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "out.txt"
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(
                        type="tool_call",
                        tool_call={
                            "id": "call_1",
                            "name": "Write",
                            "arguments": {"path": str(target), "content": "no write yet"},
                        },
                    ),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("Write file")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "waiting_approval"
    assert result.session.status == "waiting_approval"
    assert not target.exists()
    assert "approval_requested" in [event.event_type for event in snapshot.events]
    assert store.load_index()["sessions"][0]["status"] == "waiting_approval"


@pytest.mark.asyncio
async def test_runtime_returns_active_run_instead_of_starting_second_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "out.txt"
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(
                        type="tool_call",
                        tool_call={
                            "id": "call_1",
                            "name": "Write",
                            "arguments": {"path": str(target), "content": "pending"},
                        },
                    ),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    pending = await runtime.run("Write file")
    second = await runtime.run("Another prompt", session_id=pending.session.session_id)
    snapshot = store.load_session(pending.session.session_id)

    assert second.run.run_id == pending.run.run_id
    assert second.run.status == "waiting_approval"
    assert [run.status for run in snapshot.runs] == ["waiting_approval"]
    assert [message.text_content for message in snapshot.messages] == ["Write file", ""]
    assert snapshot.session.metadata["follow_up_queue"][0]["prompt"] == "Another prompt"


@pytest.mark.asyncio
async def test_runtime_drains_queued_follow_up_after_active_run_completes(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = GateProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="gate", provider_id="gate"),
        ),
        tools=_tools(),
    )

    first_task = asyncio.create_task(runtime.run("First prompt"))
    session = await _wait_for_active_session(store)
    await asyncio.wait_for(provider.first_started.wait(), timeout=1)

    queued = await runtime.run("Second prompt", session_id=session.session_id)
    queued_snapshot = store.load_session(session.session_id, recover=False)
    provider.release_first.set()
    first = await asyncio.wait_for(first_task, timeout=2)
    snapshot = store.load_session(session.session_id)

    assert queued.run.run_id == session.last_run_id
    assert queued.events[-1].event_type == "follow_up_input_queued"
    assert first.final_text == "first"
    assert provider.calls == 2
    assert "follow_up_queue" not in snapshot.session.metadata
    assert [run.status for run in snapshot.runs] == ["completed", "completed"]
    assert [message.text_content for message in snapshot.messages] == [
        "First prompt",
        "first",
        "Second prompt",
        "second",
    ]
    assert queued_snapshot.session.metadata["follow_up_queue"][0]["prompt"] == "Second prompt"


@pytest.mark.asyncio
async def test_runtime_acknowledges_follow_up_with_an_already_durable_user_message(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
    )
    session = runtime.sessions.create(title="Crash window")
    queued_session, _ = runtime.follow_ups.enqueue(
        snapshot=store.load_session(session.session_id, recover=False),
        prompt="Queued prompt",
        title=None,
    )
    queued = runtime.follow_ups.peek(store.load_session(queued_session.session_id, recover=False))
    assert queued is not None
    store.append_message(
        MessageRecord.text(
            session_id=session.session_id,
            role="user",
            text=queued.prompt,
            sequence=1,
        ).model_copy(update={"metadata": {"queued_follow_up_id": queued.queue_id}})
    )

    result = await runtime.run("Continue", session_id=session.session_id)
    snapshot = store.load_session(session.session_id, recover=False)

    assert result.run.status == "completed"
    assert provider.calls == 1
    assert len(snapshot.runs) == 1
    assert [message.text_content for message in snapshot.messages] == [
        "Queued prompt",
        "Continue",
        "ok",
    ]
    assert "follow_up_queue" not in snapshot.session.metadata


@pytest.mark.asyncio
async def test_runtime_continues_after_auto_tool_result(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "notes.txt"
    target.write_text("important notes", encoding="utf-8")
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(
                            type="tool_call",
                            tool_call={
                                "id": "call_1",
                                "name": "Read",
                                "arguments": {"path": str(target)},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="I read the notes."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("Read file")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "completed"
    assert result.final_text == "I read the notes."
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "tool_result",
        "assistant",
    ]
    assert snapshot.messages[1].metadata["tool_calls"][0]["id"] == "call_1"
    assert snapshot.messages[2].metadata["tool_call_id"] == "call_1"
    assert snapshot.messages[2].text_content == "important notes"
    assert [event.event_type for event in snapshot.events].count("model_request_started") == 2


@pytest.mark.asyncio
async def test_runtime_records_provider_usage(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(type="text_delta", text="Hello"),
                    ProviderStreamEvent(type="done", usage=Usage(input_tokens=3, output_tokens=2)),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("Say hi")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.usage["input_tokens"] == 3
    assert result.run.usage["output_tokens"] == 2
    assert snapshot.runs[0].usage["input_tokens"] == 3
    assert "model_message_completed" in [event.event_type for event in snapshot.events]


@pytest.mark.asyncio
async def test_runtime_approval_resume_executes_tool(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "approved.txt"
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(
                            type="tool_call",
                            tool_call={
                                "id": "call_1",
                                "name": "Write",
                                "arguments": {"path": str(target), "content": "approved"},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="File written."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )
    pending = await runtime.run("Write file")

    resumed = await runtime.approve_tool(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
        approved=True,
    )
    snapshot = store.load_session(pending.session.session_id)

    assert resumed.run.status == "completed"
    assert resumed.session.status == "idle"
    assert resumed.final_text == "File written."
    assert target.read_text(encoding="utf-8") == "approved"
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "tool_result",
        "assistant",
    ]
    assert snapshot.messages[1].metadata["tool_calls"][0]["name"] == "Write"
    assert "approval_resolved" in [event.event_type for event in snapshot.events]
    assert "run_completed" in [event.event_type for event in snapshot.events]


@pytest.mark.asyncio
async def test_runtime_abort_waiting_approval_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "aborted.txt"
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(
                        type="tool_call",
                        tool_call={
                            "id": "call_1",
                            "name": "Write",
                            "arguments": {"path": str(target), "content": "nope"},
                        },
                    ),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )
    pending = await runtime.run("Write file")

    aborted = await runtime.abort_run(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
    )
    snapshot = store.load_session(pending.session.session_id)

    assert aborted.run.status == "aborted"
    assert aborted.session.status == "idle"
    assert not target.exists()
    assert snapshot.runs[0].status == "aborted"
    assert snapshot.runs[0].metadata["abort_reason"] == "User aborted"
    assert "pending_invocation" not in snapshot.runs[0].metadata
    assert "run_aborted" in [event.event_type for event in snapshot.events]


@pytest.mark.asyncio
async def test_runtime_can_continue_session_after_abort(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "aborted.txt"
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(
                            type="tool_call",
                            tool_call={
                                "id": "call_1",
                                "name": "Write",
                                "arguments": {"path": str(target), "content": "nope"},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Still here."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )
    pending = await runtime.run("Write file")

    await runtime.abort_run(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
    )
    continued = await runtime.run("Continue", session_id=pending.session.session_id)
    snapshot = store.load_session(pending.session.session_id)

    assert continued.run.status == "completed"
    assert continued.session.session_id == pending.session.session_id
    assert continued.final_text == "Still here."
    assert [run.status for run in snapshot.runs] == ["aborted", "completed"]
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_runtime_aborts_active_provider_stream(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = WaitingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="waiting", provider_id="waiting"),
        ),
        tools=_tools(),
    )

    run_task = asyncio.create_task(runtime.run("Wait for abort"))
    session = await _wait_for_active_session(store)
    await _wait_for_event(store, session.session_id, "model_text_delta")

    await runtime.abort_run(
        session_id=session.session_id,
        run_id=session.last_run_id,
    )
    result = await asyncio.wait_for(run_task, timeout=1)
    snapshot = store.load_session(session.session_id)

    assert result.run.status == "aborted"
    assert result.session.status == "idle"
    assert result.final_text == "begin"
    assert provider.closed is True
    assert "run_aborted" in [event.event_type for event in snapshot.events]
    assert "run_completed" not in [event.event_type for event in snapshot.events]


@pytest.mark.asyncio
async def test_runtime_recovery_materializes_inner_loop_checkpoint(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = WaitingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="waiting", provider_id="waiting"),
        ),
        tools=_tools(),
    )

    run_task = asyncio.create_task(runtime.run("Wait for crash"))
    session = await _wait_for_active_session(store)
    await _wait_for_checkpoint(store, session.session_id, "begin")

    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    recovered = store.load_session(session.session_id)

    assert recovered.runs[0].status == "recoverable"
    assert recovered.session.status == "idle"
    assert [message.role for message in recovered.messages] == ["user", "assistant"]
    assert recovered.messages[-1].text_content == "begin"
    assert recovered.messages[-1].metadata["recovered_from_checkpoint"] is True
    assert recovered.session.message_count == 2
    assert recovered.session.last_message_id == recovered.messages[-1].message_id


@pytest.mark.asyncio
async def test_runtime_aborts_active_approved_bash_tool(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    command = f'"{sys.executable}" -c "import time; time.sleep(5); print(\'done\')"'
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(
                        type="tool_call",
                        tool_call={
                            "id": "bash_1",
                            "name": "Bash",
                            "arguments": {"command": command, "timeout_seconds": 30},
                        },
                    ),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )
    pending = await runtime.run("Run command")

    approval_task = asyncio.create_task(
        runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approved=True,
        )
    )
    for _ in range(100):
        snapshot = store.load_session(pending.session.session_id, recover=False)
        if "tool_call_started" in [event.event_type for event in snapshot.events]:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("bash tool did not start")

    await runtime.abort_run(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
    )
    result = await asyncio.wait_for(approval_task, timeout=2)
    snapshot = store.load_session(pending.session.session_id)

    assert result.run.status == "aborted"
    assert result.session.status == "idle"
    assert "run_aborted" in [event.event_type for event in snapshot.events]
    assert not any(message.role == "tool_result" for message in snapshot.messages)


@pytest.mark.asyncio
async def test_runtime_aborts_plugin_tool_through_execution_context(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    registry = _tools()
    started = asyncio.Event()

    async def wait_tool(
        invocation,
        *,
        context: ToolExecutionContext,
    ) -> ToolResult:
        started.set()
        await context.sleep(5)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="not reached",
        )

    registry.register(
        Tool(
            definition=ToolDefinition(
                name="WaitPlugin",
                description="Wait until cancelled.",
                source="third_party_plugin",
                permission_mode="auto",
            ),
            handler=wait_tool,
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(
                        type="tool_call",
                        tool_call={
                            "id": "wait_1",
                            "name": "WaitPlugin",
                            "arguments": {},
                        },
                    ),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=registry,
    )

    run_task = asyncio.create_task(runtime.run("Run plugin tool"))
    session = await _wait_for_active_session(store)
    await asyncio.wait_for(started.wait(), timeout=1)

    await runtime.abort_run(
        session_id=session.session_id,
        run_id=session.last_run_id,
    )
    result = await asyncio.wait_for(run_task, timeout=1)
    snapshot = store.load_session(session.session_id)

    assert result.run.status == "aborted"
    assert result.session.status == "idle"
    assert "run_aborted" in [event.event_type for event in snapshot.events]
    assert not any(
        message.metadata.get("tool_name") == "WaitPlugin" for message in snapshot.messages
    )


@pytest.mark.asyncio
async def test_runtime_approval_rejection_writes_tool_error(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "rejected.txt"
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(
                            type="tool_call",
                            tool_call={
                                "id": "call_1",
                                "name": "Write",
                                "arguments": {"path": str(target), "content": "nope"},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="I will not write it."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )
    pending = await runtime.run("Write file")

    resumed = await runtime.approve_tool(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
        approved=False,
        reason="not today",
    )
    snapshot = store.load_session(pending.session.session_id)

    assert resumed.run.status == "completed"
    assert resumed.final_text == "I will not write it."
    assert not target.exists()
    assert snapshot.messages[1].metadata["tool_calls"][0]["name"] == "Write"
    assert snapshot.messages[2].text_content == "not today"
    assert snapshot.messages[-1].text_content == "I will not write it."
    assert resumed.tool_results[0].is_error is True


@pytest.mark.asyncio
async def test_runtime_answer_user_resumes_waiting_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent(
                            type="tool_call",
                            tool_call={
                                "id": "ask_1",
                                "name": "AskUser",
                                "arguments": {"question": "Which file?"},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="I will use notes.md."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )
    pending = await runtime.run("Need clarification")

    resumed = await runtime.answer_user(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
        answer="notes.md",
    )
    snapshot = store.load_session(pending.session.session_id)

    assert pending.run.status == "waiting_user"
    assert resumed.run.status == "completed"
    assert resumed.final_text == "I will use notes.md."
    assert snapshot.messages[1].role == "assistant"
    assert snapshot.messages[1].metadata["tool_calls"][0]["name"] == "AskUser"
    assert snapshot.messages[2].role == "tool_result"
    assert snapshot.messages[2].text_content == "notes.md"
    assert snapshot.messages[-1].text_content == "I will use notes.md."
    assert "ask_user_resolved" in [event.event_type for event in snapshot.events]


class ReasoningToolProvider:
    provider_id = "reasoning_tool"

    def __init__(self, read_path: str = "data.txt") -> None:
        self.calls = 0
        self.messages: list[list[MessageRecord]] = []
        self.system_prompts: list[str] = []
        self.read_path = read_path

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages,
        tools=None,
    ):
        del model, tools
        self.calls += 1
        self.messages.append(list(messages))
        self.system_prompts.append(system_prompt)
        if self.calls == 1:
            yield ProviderStreamEvent(
                type="reasoning_delta",
                reasoning_content="I should read the file.",
            )
            yield ProviderStreamEvent(
                type="tool_call",
                tool_call={
                    "id": "call_1",
                    "name": "Read",
                    "arguments": {"path": self.read_path},
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="Done")
        yield ProviderStreamEvent(type="done")

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


@pytest.mark.asyncio
async def test_runtime_preserves_reasoning_content_for_next_model_call(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    (tmp_path / "data.txt").write_text("hello", encoding="utf-8")
    provider = ReasoningToolProvider(read_path=str(tmp_path / "data.txt"))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="reasoning_tool", provider_id="reasoning_tool"),
        ),
        tools=_tools(),
        context_builder=ContextBuilder(skill_instructions="Available skills."),
        system_prompt="System instructions.",
    )

    result = await runtime.run("Read and summarize")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "completed"
    assert len(provider.messages) == 2
    assert provider.system_prompts == [
        "System instructions.\n\nAvailable skills.",
        "System instructions.\n\nAvailable skills.",
    ]
    second_call_messages = provider.messages[1]
    assistant_messages = [m for m in second_call_messages if m.role == "assistant"]
    assert len(assistant_messages) == 1
    assistant_message = assistant_messages[0]
    assert assistant_message.metadata.get("reasoning_content") == "I should read the file."
    assert assistant_message.metadata.get("tool_calls")
    assert snapshot.messages[-1].text_content == "Done"
