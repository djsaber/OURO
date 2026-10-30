import asyncio
import sys
from threading import Event, get_ident

import pytest

from ouro.application.resources import ResourceService
from ouro.application.runs.lifecycle import abort_run_state
from ouro.application.runs.tool_batches import load_tool_batch
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.application.sessions.service import SessionService
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.adapter import RuntimeAdapter
from ouro.core.context import ContextBuilder
from ouro.core.context_providers import (
    ContextFragment,
    ContextProviderRegistration,
    ContextProviderRegistry,
    ContextProviderResult,
)
from ouro.core.errors import (
    PersistenceError,
    PromptRejectedError,
    ResourceNotFoundError,
    SessionBusyError,
)
from ouro.core.messages import MessageRecord, ModelMessage
from ouro.core.observers import Observation, ObserverRegistration, ObserverRegistry
from ouro.core.prompt_policies import (
    PromptPolicyCall,
    PromptPolicyDecision,
    PromptPolicyRegistration,
    PromptPolicyRegistry,
)
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderRetryNotice,
    ProviderStreamEvent,
    ToolCallDelta,
    Usage,
)
from ouro.core.results import RunResult
from ouro.core.run_budget import RunBudgetPolicy
from ouro.core.session_controls import AttributedSessionInput
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tool_batches import ToolBatchCall, ToolCallBatchRecord
from ouro.core.tools import ToolCallRequest, ToolInvocation
from ouro.core.turn_policies import (
    TurnPolicyDecision,
    TurnPolicyRegistration,
    TurnPolicyRegistry,
)
from ouro.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    SUMMARIZATION_PROMPT,
    render_agent_skills_prompt,
)
from ouro.providers import FauxProvider
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.skills import SkillMetadata
from ouro.tools import (
    Tool,
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    builtin_tools,
)
from ouro.tools.builtin.shell import detect_command_shell
from tests.application.helpers import running_runtime_task
from tests.event_capture import recorded_events
from tests.run_facts import run_events, run_tool_results
from tests.tool_batches import bind_tool_messages, persist_tool_batch


def _tools() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    return registry


@pytest.mark.asyncio
async def test_shared_agent_budget_query_runs_outside_the_event_loop(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider([ProviderStreamEvent(type="done")]),
            ModelMetadata(model_id="shared-budget", provider_id="faux"),
        ),
        tools=ToolRegistry(),
    )
    run = RunRecord(session_id="ses_shared_budget", status="running")
    entered = Event()
    release = Event()
    caller_thread = get_ident()
    worker_threads: list[int] = []

    class AgentControls:
        def budget_reason(self, candidate: RunRecord, next_model_round: int):
            assert get_ident() != caller_thread
            assert candidate == run
            assert next_model_round == 2
            worker_threads.append(get_ident())
            entered.set()
            release.wait()
            return None

    monkeypatch.setattr(runtime, "_agent_controls", AgentControls())
    checking = asyncio.create_task(runtime._shared_budget_reason(run, 2))
    try:
        assert await asyncio.to_thread(entered.wait, 30) is True
        assert checking.done() is False
    finally:
        release.set()
        await asyncio.gather(checking, return_exceptions=True)
    assert await checking is None
    assert worker_threads != [caller_thread]


@pytest.mark.asyncio
async def test_requested_abort_finalization_reads_store_outside_the_event_loop(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    run = RunRecord(
        session_id="ses_finalize_abort",
        status="running",
        abort_requested=True,
        metadata={"abort_reason": "stop"},
    )
    session = SessionRecord(
        session_id=run.session_id,
        status="running",
        active_run_id=run.run_id,
    )
    store.create_session(session)
    store.save_run(run)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider([ProviderStreamEvent(type="done")]),
            ModelMetadata(model_id="abort-finalize", provider_id="faux"),
        ),
        tools=ToolRegistry(),
    )
    entered = Event()
    release = Event()
    caller_thread = get_ident()
    reader_threads: list[int] = []
    load_run = store.load_run

    def blocking_load_run(session_id: str, run_id: str) -> RunRecord:
        assert get_ident() != caller_thread
        reader_threads.append(get_ident())
        entered.set()
        release.wait()
        return load_run(session_id, run_id)

    monkeypatch.setattr(store, "load_run", blocking_load_run)
    finalizing = asyncio.create_task(
        runtime._finalize_requested_abort(RunResult(session=session, run=run))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 30) is True
        assert finalizing.done() is False
    finally:
        release.set()
        await asyncio.gather(finalizing, return_exceptions=True)
    result = await finalizing
    assert result.run.status == "aborted"
    assert reader_threads
    assert caller_thread not in reader_threads


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_runtime_durable_boundaries_do_not_block_the_event_loop(
    tmp_path,
    backend,
    monkeypatch,
) -> None:
    store = (
        LocalSessionStore(tmp_path / "files")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    )
    store.initialize()
    tools = ToolRegistry()

    def probe(invocation: ToolInvocation, context) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="ok",
        )

    tools.register(
        Tool(
            definition=ToolDefinition(
                name="Probe",
                description="Exercise durable Runtime boundaries.",
                permission_mode="auto",
            ),
            handler=probe,
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {"id": "probe_1", "name": "Probe", "arguments": {}}
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="finished"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="persistence-boundary", provider_id="faux"),
        ),
        tools=tools,
    )

    loop = asyncio.get_running_loop()
    main_thread_id = get_ident()
    observations: dict[str, int] = {}

    original_commit = store.commit_session

    def delay(label: str) -> None:
        if label in observations:
            return
        assert get_ident() != main_thread_id
        responded = Event()
        loop.call_soon_threadsafe(responded.set)
        assert responded.wait(30), "event loop stalled while a persistence boundary was held"
        observations[label] = get_ident()

    def commit(request):
        if request.request_manifest is not None:
            delay("context_snapshot")
        if any(run.active_tool_batch is not None for run in request.runs):
            delay("tool_checkpoint")
        if any(run.status == "completed" for run in request.runs):
            delay("run_terminal")
        return original_commit(request)

    monkeypatch.setattr(store, "commit_session", commit)
    try:
        result = await runtime.run("Use the probe")
    finally:
        await runtime.close()

    assert result.final_text == "finished"
    assert observations.keys() == {"context_snapshot", "tool_checkpoint", "run_terminal"}
    for thread_id in observations.values():
        assert thread_id != main_thread_id


def test_abort_state_does_not_load_message_history(tmp_path, monkeypatch) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    run = RunRecord(session_id="ses_abort", status="running")
    session = SessionRecord(
        session_id=run.session_id,
        status="running",
        active_run_id=run.run_id,
    )
    store.create_session(session)
    store.save_run(run)

    def reject_runtime_aggregate(*_args, **_kwargs):
        raise AssertionError("abort must inspect only Session and Run state")

    monkeypatch.setattr(store, "load_runtime_session", reject_runtime_aggregate)

    idle, aborted, changed = abort_run_state(
        store,
        session_id=session.session_id,
        run_id=run.run_id,
        reason="stop",
    )

    assert [event.event_type for event in changed] == ["run_aborted"]
    assert idle.status == "idle"
    assert aborted.status == "aborted"


@pytest.mark.asyncio
async def test_runtime_produces_each_public_observation_type(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    tools = ToolRegistry()

    def inspect_tool(invocation: ToolInvocation, context) -> ToolResult:
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="observed",
        )

    tools.register(
        Tool(
            definition=ToolDefinition(
                name="ObserveTool",
                description="Exercise typed observation producers.",
                permission_mode="auto",
            ),
            handler=inspect_tool,
        )
    )
    seen: list[Observation] = []
    observers = ObserverRegistry()
    observers.register_many(
        tuple(
            ObserverRegistration(
                plugin_id="dev.ouro.observation_test",
                observer_id=event,
                event=event,
                handler=seen.append,
            )
            for event in (
                "runtime_event",
                "context_prepared",
                "tool_call_requested",
                "tool_call_finished",
                "run_finished",
            )
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "observe_1",
                                "name": "ObserveTool",
                                "arguments": {"value": 1},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="finished"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="observations", provider_id="faux"),
        ),
        tools=tools,
        observers=observers,
    )

    result = await runtime.run("Exercise observations")
    await runtime.event_recorder.drain()

    assert result.run.status == "completed"
    assert {observation.event for observation in seen} == {
        "runtime_event",
        "context_prepared",
        "tool_call_requested",
        "tool_call_finished",
        "run_finished",
    }
    await runtime.close()


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
        request_context=None,
    ):
        del model, system_prompt, messages, tools
        try:
            yield ProviderStreamEvent(type="text_delta", text="begin")
            while True:
                await asyncio.sleep(1)
        finally:
            self.closed = True

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
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
        request_context=None,
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

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class ToolDeltaGateProvider:
    provider_id = "tool-delta-gate"

    def __init__(self) -> None:
        self.waiting_after_first_delta = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages,
        tools=None,
        request_context=None,
    ):
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(
                index=0,
                id_delta="call_1",
                name_delta="Write",
                arguments_delta='{"path":"out.txt",',
            ),
        )
        self.waiting_after_first_delta.set()
        await self.release.wait()
        yield ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(
                index=0,
                arguments_delta='"content":"hello"}',
            ),
        )
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
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
        request_context=None,
    ):
        del model, system_prompt, messages, tools
        yield ProviderStreamEvent(type="text_delta", text="partial")
        raise RuntimeError("stream broke")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


class CapturingProvider:
    provider_id = "capture"

    def __init__(self) -> None:
        self.messages: list[ModelMessage] = []
        self.system_prompt = ""
        self.tools: list[dict[str, object]] = []
        self.calls = 0

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages,
        tools=None,
        request_context=None,
    ):
        del model
        self.calls += 1
        self.system_prompt = system_prompt
        self.messages = list(messages)
        self.tools = list(tools or [])
        yield ProviderStreamEvent(type="text_delta", text="ok")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


async def _wait_for_active_session(store: LocalSessionStore):
    async with asyncio.timeout(30):
        while True:
            sessions = store.list_sessions()
            if sessions and sessions[0].last_run_id:
                return sessions[0]
            await asyncio.sleep(0.01)


async def _wait_for_event(store: LocalSessionStore, session_id: str, event_type: str) -> None:
    async with asyncio.timeout(30):
        while True:
            snapshot = store.load_session(session_id)
            if any(
                event.event_type == event_type
                for event in recorded_events(snapshot.session.session_id)
            ):
                return
            await asyncio.sleep(0.01)


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
async def test_runtime_appends_additional_system_prompt(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="capture", provider_id="capture")),
        tools=_tools(),
        context_builder=ContextBuilder(skill_instructions="Available skills."),
        system_prompt="Base system prompt.",
        additional_system_prompt="Additional system prompt.",
    )

    await runtime.run("hello")

    assert provider.system_prompt == (
        "Base system prompt.\n\nAdditional system prompt.\n\nAvailable skills."
    )
    assert provider.messages[0].role == "user"


@pytest.mark.asyncio
async def test_prompt_policy_denies_before_message_and_run_persistence(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionService(store).create(title="Policy boundary").session
    calls: list[PromptPolicyCall] = []

    def deny(call: PromptPolicyCall) -> PromptPolicyDecision:
        calls.append(call)
        return PromptPolicyDecision(
            action="deny",
            reason=f"rejected {call.prompt}; token=policy-reason-secret",
        )

    policies = PromptPolicyRegistry()
    policies.register(
        PromptPolicyRegistration(
            plugin_id="dev.ouro.guard",
            policy_id="secret",
            handler=deny,
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="policy", provider_id="faux"),
        ),
        tools=_tools(),
        prompt_policies=policies,
        default_workspace=tmp_path,
    )

    with pytest.raises(PromptRejectedError) as rejected:
        await runtime.run("do not persist this prompt", session_id=session.session_id)

    assert rejected.value.reason == "rejected [REDACTED]; token=***"
    assert rejected.value.plugin_id == "dev.ouro.guard"
    assert rejected.value.policy_id == "secret"
    assert calls[0].prompt == "do not persist this prompt"
    assert calls[0].workspace == str(tmp_path.resolve())
    snapshot = store.load_session(session.session_id)
    assert snapshot.messages == []
    assert snapshot.runs == []
    assert "do not persist this prompt" not in session.model_dump_json()

    with pytest.raises(PromptRejectedError):
        await runtime.run("new-session prompt must not become a title")
    assert [item.session_id for item in store.list_sessions()] == [session.session_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["timeout", "exception", "invalid"])
async def test_prompt_policy_failures_close_before_canonical_state(
    tmp_path,
    failure_mode: str,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionService(store).create(title="Fail closed").session

    async def policy(call: PromptPolicyCall):
        del call
        if failure_mode == "timeout":
            await asyncio.sleep(1)
            return PromptPolicyDecision()
        if failure_mode == "exception":
            raise RuntimeError("token=prompt-policy-secret")
        return None

    policies = PromptPolicyRegistry()
    policies.register(
        PromptPolicyRegistration(
            "dev.ouro.guard",
            failure_mode,
            policy,
            timeout_seconds=0.01,
        )
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="policy", provider_id="faux"),
        ),
        tools=_tools(),
        prompt_policies=policies,
    )

    with pytest.raises(PromptRejectedError) as rejected:
        await runtime.run("unpersisted failure input", session_id=session.session_id)

    assert rejected.value.policy_failed is True
    assert rejected.value.failure_error_type in {"TimeoutError", "RuntimeError", "TypeError"}
    assert "unpersisted failure input" not in (rejected.value.failure_message or "")
    snapshot = store.load_session(session.session_id)
    assert snapshot.messages == []
    assert snapshot.runs == []
    assert "unpersisted failure input" not in snapshot.session.model_dump_json()


@pytest.mark.asyncio
async def test_prompt_policy_allow_precedes_normal_run_creation(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    calls: list[str] = []

    def allow(call: PromptPolicyCall) -> PromptPolicyDecision:
        calls.append(call.prospective_run_id)
        return PromptPolicyDecision()

    policies = PromptPolicyRegistry()
    policies.register(PromptPolicyRegistration("ouro.host", "allow", allow))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="policy", provider_id="faux"),
        ),
        tools=_tools(),
        prompt_policies=policies,
    )

    result = await runtime.run("allowed prompt")

    assert calls == [result.run.run_id]
    snapshot = store.load_session(result.session.session_id)
    assert snapshot.messages[0].text_content == "allowed prompt"
    assert snapshot.runs[0].run_id == result.run.run_id


@pytest.mark.asyncio
async def test_prompt_policy_excludes_plugin_input_and_human_resolutions(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "approved.txt"
    inspected_prompts: list[str] = []

    def inspect(call: PromptPolicyCall) -> PromptPolicyDecision:
        inspected_prompts.append(call.prompt)
        return PromptPolicyDecision()

    policies = PromptPolicyRegistry()
    policies.register(PromptPolicyRegistration("ouro.host", "inspect", inspect))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "write_1",
                                "name": "Write",
                                "arguments": {
                                    "path": str(target),
                                    "content": "approved",
                                },
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="File written."),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "ask_1",
                                "name": "AskUser",
                                "arguments": {
                                    "questions": [
                                        {
                                            "id": "file",
                                            "header": "File",
                                            "question": "Which file?",
                                            "options": [
                                                {
                                                    "label": "notes.md (Recommended)",
                                                    "description": "Use the notes file.",
                                                },
                                                {
                                                    "label": "README.md",
                                                    "description": "Use the readme file.",
                                                },
                                            ],
                                        }
                                    ]
                                },
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Answer accepted."),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Plugin input accepted."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="policy-boundaries", provider_id="faux"),
        ),
        tools=_tools(),
        prompt_policies=policies,
    )

    waiting_approval = await runtime.run("Write the approved file")
    assert inspected_prompts == ["Write the approved file"]
    assert waiting_approval.run.pending_approval_id is not None
    await runtime.approve_tool(
        session_id=waiting_approval.session.session_id,
        run_id=waiting_approval.run.run_id,
        approval_id=waiting_approval.run.pending_approval_id,
        approved=True,
    )
    assert inspected_prompts == ["Write the approved file"]

    waiting_user = await runtime.run("Ask for a file")
    assert inspected_prompts == ["Write the approved file", "Ask for a file"]
    await runtime.answer_user(
        session_id=waiting_user.session.session_id,
        run_id=waiting_user.run.run_id,
        answer={"answers": {"file": ["notes.md"]}},
    )
    assert inspected_prompts == ["Write the approved file", "Ask for a file"]

    plugin_result = await runtime.submit_plugin_input(
        AttributedSessionInput(
            prompt="Scaffold command output",
            plugin_id="dev.ouro.command",
            contribution_id="start",
            generation_id="gen_command",
            execution_contract_hash="contract_command",
        ),
        session_id=None,
    )
    assert plugin_result.run.status == "completed"
    assert inspected_prompts == ["Write the approved file", "Ask for a file"]
    plugin_snapshot = store.load_session(plugin_result.session.session_id)
    assert plugin_snapshot.messages[0].role == "user"


@pytest.mark.asyncio
async def test_prompt_policy_denies_before_active_run_queue_persistence(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = GateProvider()

    def policy(call: PromptPolicyCall) -> PromptPolicyDecision:
        if call.prompt == "blocked follow-up":
            return PromptPolicyDecision(action="deny", reason="blocked before queue")
        return PromptPolicyDecision()

    policies = PromptPolicyRegistry()
    policies.register(PromptPolicyRegistration("ouro.host", "queue-guard", policy))
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="policy", provider_id="gate"),
        ),
        tools=_tools(),
        prompt_policies=policies,
    )
    async with running_runtime_task(runtime, runtime.run("first prompt")) as first:
        await provider.first_started.wait()
        session = await _wait_for_active_session(store)

        with pytest.raises(PromptRejectedError, match="blocked before queue"):
            await runtime.run("blocked follow-up", session_id=session.session_id)

        snapshot = store.load_session(session.session_id)
        assert not snapshot.session.pending_input_ids
        assert all(message.text_content != "blocked follow-up" for message in snapshot.messages)
        provider.release_first.set()
        await first


@pytest.mark.asyncio
async def test_runtime_persists_prepared_resource_content_in_attachment_scaffold(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    resource_store = LocalResourceStore(store.root)
    resource_store.initialize()
    resources = ResourceService(store=resource_store, sessions=store)
    session = SessionService(store, resources=resources).create(title="Attachments").session
    source = tmp_path / "brief.txt"
    source.write_text("projected attachment", encoding="utf-8")
    resource = resources.import_resource(session.session_id, source).resource
    provider = CapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
        resources=resources,
    )

    result = await runtime.run(
        "Use the brief",
        session_id=session.session_id,
        resource_refs=[resource.resource_id],
    )
    snapshot = store.load_session(session.session_id)

    assert result.run.status == "completed"
    assert "projected attachment" in provider.messages[0].text_content
    assert not hasattr(provider.messages[0], "resource_refs")
    assert snapshot.messages[0].semantic_tag == "attachments"
    assert snapshot.messages[1].text_content == "Use the brief"
    assert snapshot.messages[0].resource_refs == [resource.resource_id]
    assert "projected attachment" in snapshot.messages[0].text_content
    prepared = runtime.sessions.context(session.session_id)
    assert prepared is not None
    assert prepared.status.completed_at is not None
    assert prepared.status.provider_reported_input_tokens is None
    assert prepared.provider_context.system_prompt == provider.system_prompt
    assert prepared.provider_context.messages == provider.messages
    assert prepared.provider_context.tools == provider.tools


@pytest.mark.asyncio
async def test_runtime_validates_resource_before_starting_run_or_message(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    resource_store = LocalResourceStore(store.root)
    resource_store.initialize()
    resources = ResourceService(store=resource_store, sessions=store)
    session = SessionService(store, resources=resources).create(title="Attachments").session
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider([ProviderStreamEvent(type="done")]),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
        resources=resources,
    )

    with pytest.raises(ResourceNotFoundError, match="not attached"):
        await runtime.run(
            "Use missing",
            session_id=session.session_id,
            resource_refs=["res_" + "0" * 32],
        )

    snapshot = store.load_session(session.session_id)
    assert snapshot.messages == []
    assert snapshot.runs == []


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
    assert snapshot.messages[0].run_id == result.run.run_id
    assert snapshot.messages[0].turn_id is None
    assert snapshot.messages[-1].run_id == result.run.run_id
    assert snapshot.messages[-1].turn_id is not None
    assert "run_completed" in [
        event.event_type for event in recorded_events(snapshot.session.session_id)
    ]


@pytest.mark.asyncio
async def test_runtime_hot_path_does_not_load_complete_history_snapshot(tmp_path) -> None:
    class RuntimeOnlyStore(LocalSessionStore):
        def load_session(self, session_id: str):
            raise AssertionError("runtime hot path loaded replay events")

    store = RuntimeOnlyStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("Say hi")

    assert result.run.status == "completed"


@pytest.mark.asyncio
async def test_existing_session_loads_execution_metadata_once_before_provider_stream(
    tmp_path,
) -> None:
    class CountingStore(LocalSessionStore):
        runtime_snapshot_loads = 0

        def load_runtime_session(self, session_id: str):
            self.runtime_snapshot_loads += 1
            return super().load_runtime_session(session_id)

    class LoadObservingProvider(CapturingProvider):
        loads_at_stream_start: int | None = None

        async def stream(self, **kwargs):
            self.loads_at_stream_start = store.runtime_snapshot_loads
            async for event in super().stream(**kwargs):
                yield event

    store = CountingStore(tmp_path)
    store.initialize()
    session = SessionService(store).create(title="Existing").session
    store.runtime_snapshot_loads = 0
    provider = LoadObservingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
    )

    result = await runtime.run(
        "Say hi",
        session_id=session.session_id,
        approval_mode="auto",
    )

    assert result.run.status == "completed"
    assert provider.loads_at_stream_start == 1


@pytest.mark.asyncio
async def test_runtime_observes_tool_arguments_live_and_persists_only_complete_calls(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = ToolDeltaGateProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="tool-delta-gate", provider_id=provider.provider_id),
        ),
        tools=_tools(),
    )

    seen = []
    delta_seen = asyncio.Event()

    def observe(event):
        if event.event_type == "model_tool_call_delta":
            seen.append(event)
            delta_seen.set()

    runtime.add_event_sink(observe)
    async with running_runtime_task(runtime, runtime.run("Write a file")) as run_task:
        await provider.waiting_after_first_delta.wait()
        session = store.list_sessions()[0]
        await asyncio.wait_for(delta_seen.wait(), timeout=30)
        assert not any(
            event.event_type == "model_tool_call_delta"
            for event in recorded_events(session.session_id)
        )
        delta_events = seen
        assert run_task.done() is False
        assert [event.payload["arguments_delta"] for event in delta_events] == [
            '{"path":"out.txt",'
        ]

        provider.release.set()
        result = await run_task
        completed_snapshot = store.load_session(session.session_id)
        completed_delta_events = [
            event
            for event in recorded_events(completed_snapshot.session.session_id)
            if event.event_type == "model_tool_call_delta"
        ]

        assert result.run.status == "waiting_approval"
        assert result.run.active_tool_batch is not None
        assert load_tool_batch(store, result.run).calls[0].request.arguments == {
            "path": "out.txt",
            "content": "hello",
        }
        assert completed_delta_events == []
        await runtime.event_recorder.drain()
        assert len(seen) == 2


@pytest.mark.asyncio
async def test_runtime_rejects_incomplete_streamed_tool_arguments_before_batch_creation(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(
                        type="tool_call_delta",
                        tool_call_delta=ToolCallDelta(
                            index=0,
                            id_delta="call_1",
                            name_delta="Write",
                            arguments_delta='{"path":',
                        ),
                    ),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("Write a file")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "failed"
    assert "tool call arguments are not valid JSON" in (result.run.error or "")
    assert "model_tool_call_delta" not in [
        event.event_type for event in recorded_events(snapshot.session.session_id)
    ]
    assert "tool_batch_planned" not in [
        event.event_type for event in recorded_events(snapshot.session.session_id)
    ]


@pytest.mark.asyncio
async def test_runtime_pauses_at_configured_soft_round_budget(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "note.txt"
    target.write_text("content", encoding="utf-8")
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "call_1",
                                "name": "Read",
                                "arguments": {"path": str(target)},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="not reached"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
        run_budget=RunBudgetPolicy(max_model_rounds=1),
        default_workspace=tmp_path,
    )

    result = await runtime.run("Read once")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "paused"
    assert result.run.model_round_count == 1
    assert result.session.status == "idle"
    assert result.run.metadata["pause"]["reason"] == "model_rounds"
    assert store.read_catalog().sessions[0].status == "paused"
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "tool_result",
    ]
    assert recorded_events(snapshot.session.session_id)[-1].event_type == "run_paused"


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
    assert snapshot.messages[-1].run_id == result.run.run_id
    assert snapshot.messages[-1].turn_id is not None
    assert snapshot.session.message_count == 2
    assert snapshot.session.last_message_id == snapshot.messages[-1].message_id
    assert recorded_events(snapshot.session.session_id)[-1].event_type == "run_failed"
    index = store.read_catalog()
    assert index.sessions[0].status == "failed"


@pytest.mark.asyncio
async def test_next_run_projects_interrupted_assistant_as_recovery_context(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    first_runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            ExplodingProvider(),
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
    )
    failed = await first_runtime.run("Start work")
    provider = CapturingProvider()
    second_runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
    )

    resumed = await second_runtime.run(
        "Continue",
        session_id=failed.session.session_id,
    )

    assert resumed.run.status == "completed"
    assert [message.role for message in provider.messages] == [
        "user",
        "system",
        "user",
    ]
    assert "unfinished" in provider.messages[1].text_content
    assert not hasattr(provider.messages[1], "metadata")


@pytest.mark.asyncio
async def test_runtime_reconciles_interrupted_tool_exchange_before_next_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Recover tool", status="running")
    run = RunRecord(
        session_id=session.session_id,
        status="running",
        phase="tool_execution",
    )
    request = ToolCallRequest(id="call_1", name="Write", arguments={"path": "note.txt"})
    invocation = ToolInvocation(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn_1",
        tool_call_id=request.id,
        tool_name=request.name,
        arguments=request.arguments,
    )
    assistant = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn_1",
        role="assistant",
        text="",
        sequence=1,
    ).model_copy(update={"tool_calls": [request]})
    batch = ToolCallBatchRecord(
        turn_id="turn_1",
        assistant_message_id=assistant.message_id,
        phase="executing",
        calls=[
            ToolBatchCall(
                request=request,
                status="running",
                invocation=invocation,
            )
        ],
    )
    run = run.model_copy(update={"active_tool_batch": batch.head()})
    session = session.model_copy(
        update={
            "active_run_id": run.run_id,
            "last_run_id": run.run_id,
            "message_count": 1,
            "last_message_id": assistant.message_id,
        }
    )
    store.create_session(session)
    run = persist_tool_batch(store, run, batch)
    bind_tool_messages(store, run, batch, [assistant])
    store.append_message(assistant)
    provider = CapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("Continue safely", session_id=session.session_id)
    snapshot = store.load_session(session.session_id)

    assert result.run.status == "completed"
    recovered_result = snapshot.messages[1]
    assert recovered_result.role == "tool_result"
    assert recovered_result.tool_result is not None
    assert recovered_result.tool_result.tool_call_id == "call_1"
    assert "outcome is unknown" in recovered_result.text_content
    assert [message.role for message in provider.messages[:3]] == [
        "assistant",
        "tool_result",
        "user",
    ]


@pytest.mark.asyncio
async def test_runtime_reconciles_failed_tool_exchange_before_next_run(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Failed tool", status="failed")
    run = RunRecord(
        session_id=session.session_id,
        status="failed",
        phase="tool_execution",
        error="tool batch handling failed",
    )
    request = ToolCallRequest(id="call_failed", name="Read", arguments={"path": "note.txt"})
    assistant = MessageRecord.text(
        session_id=session.session_id,
        run_id=run.run_id,
        turn_id="turn_failed",
        role="assistant",
        text="",
        sequence=1,
    ).model_copy(update={"tool_calls": [request]})
    session = session.model_copy(
        update={
            "last_run_id": run.run_id,
            "message_count": 1,
            "last_message_id": assistant.message_id,
        }
    )
    batch = ToolCallBatchRecord(
        turn_id=assistant.turn_id,
        assistant_message_id=assistant.message_id,
        calls=[ToolBatchCall(request=request, status="ready")],
    )
    store.create_session(session)
    run = persist_tool_batch(store, run, batch)
    bind_tool_messages(store, run, batch, [assistant])
    store.append_message(assistant)
    provider = CapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("Continue", session_id=session.session_id)
    snapshot = store.load_session(session.session_id)

    assert result.run.status == "completed"
    recovered_result = snapshot.messages[1]
    assert recovered_result.role == "tool_result"
    assert recovered_result.tool_result is not None
    assert recovered_result.tool_result.tool_call_id == "call_failed"
    assert recovered_result.tool_result.is_error is True
    assert [message.role for message in provider.messages[:3]] == [
        "assistant",
        "tool_result",
        "user",
    ]


@pytest.mark.asyncio
async def test_runtime_commits_checkpointed_tool_result_during_recovery(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Recover checkpoint", status="running")
    request = ToolCallRequest(id="call_reused", name="Read", arguments={"path": "note.txt"})
    checkpointed = ToolResult(
        tool_call_id=request.id,
        tool_name=request.name,
        content="checkpointed content",
    )
    assistant = MessageRecord.text(
        session_id=session.session_id,
        run_id="run_checkpoint",
        turn_id="turn_checkpoint",
        role="assistant",
        text="",
        sequence=1,
    ).model_copy(update={"tool_calls": [request]})
    batch = ToolCallBatchRecord(
        turn_id="turn_checkpoint",
        assistant_message_id=assistant.message_id,
        phase="committing_results",
        calls=[
            ToolBatchCall(
                request=request,
                status="completed",
                result=checkpointed,
            )
        ],
    )
    interrupted = RunRecord(
        run_id="run_checkpoint",
        session_id=session.session_id,
        status="running",
        phase="result_commit",
        active_tool_batch=batch.head(),
    )
    session = session.model_copy(
        update={
            "active_run_id": interrupted.run_id,
            "last_run_id": interrupted.run_id,
            "message_count": 1,
            "last_message_id": assistant.message_id,
        }
    )
    store.create_session(session)
    interrupted = persist_tool_batch(store, interrupted, batch)
    bind_tool_messages(store, interrupted, batch, [assistant])
    store.append_message(assistant)
    provider = CapturingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
    )

    await runtime.run("Continue", session_id=session.session_id)
    snapshot = store.load_session(session.session_id)
    recovered_run = next(run for run in snapshot.runs if run.run_id == interrupted.run_id)

    assert snapshot.messages[1].text_content == "checkpointed content"
    assert recovered_run.active_tool_batch is not None
    assert load_tool_batch(store, recovered_run).calls[0].status == "completed"
    assert (
        load_tool_batch(store, recovered_run).calls[0].result_message_id
        == snapshot.messages[1].message_id
    )


def test_runtime_rejects_checkpointed_result_for_different_tool_call(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Reject invalid checkpoint")
    store.create_session(session)
    run = RunRecord(session_id=session.session_id, status="running")
    batch = ToolCallBatchRecord(
        turn_id="turn_invalid",
        assistant_message_id="message_invalid",
        phase="committing_results",
        calls=[
            ToolBatchCall(
                request=ToolCallRequest(id="call_expected", name="Read"),
                status="completed",
                result=ToolResult(
                    tool_call_id="call_other", tool_name="Write", content="wrong result"
                ),
            )
        ],
    )
    with pytest.raises(ValueError, match="Outcome identity does not match its Call"):
        persist_tool_batch(store, run, batch)
    snapshot = store.load_session(session.session_id)
    assert snapshot.tool_facts.calls == ()
    assert snapshot.tool_facts.outcomes == ()
    assert snapshot.call_states == ()
    assert snapshot.runs == []


@pytest.mark.asyncio
async def test_runtime_context_provider_adds_context_to_provider(tmp_path) -> None:
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

    provider.stream = recording_stream  # type: ignore[method-assign]
    context_providers = ContextProviderRegistry()
    context_providers.replace_owner(
        "ctx",
        (
            ContextProviderRegistration(
                plugin_id="ctx",
                provider_id="extra",
                handler=lambda _call: ContextProviderResult(
                    fragments=[ContextFragment(text="extra context")]
                ),
            ),
        ),
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(provider, ModelMetadata(model_id="faux", provider_id="faux")),
        tools=_tools(),
        context_providers=context_providers,
    )

    result = await runtime.run("Use context")

    assert seen_system_prompts == [DEFAULT_SYSTEM_PROMPT]
    assert seen_messages[0].role == "user"
    assert seen_messages[0].text_content == "extra context"
    snapshot = store.load_session(result.session.session_id)
    assert not any(
        message.metadata.get("kind") == "context_provider" for message in snapshot.messages
    )


@pytest.mark.asyncio
async def test_runtime_context_provider_appends_system_prompt_without_creating_message(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingProvider()
    seen_calls = []
    context_providers = ContextProviderRegistry()

    def provide(call):
        seen_calls.append(call)
        return ContextProviderResult(
            fragments=[
                ContextFragment(
                    text="facts",
                    semantic_tag="runtime-facts",
                    target="system_prompt",
                )
            ]
        )

    context_providers.replace_owner(
        "ctx",
        (
            ContextProviderRegistration(
                plugin_id="dev.ouro.runtime",
                provider_id="facts",
                handler=provide,
            ),
        ),
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
        context_providers=context_providers,
        default_workspace=tmp_path / "workspace",
        environment=RuntimeEnvironment.local(tmp_path, store.sessions_dir),
    )

    result = await runtime.run("Use runtime facts")

    assert provider.system_prompt.endswith("<runtime-facts>\nfacts\n</runtime-facts>")
    assert seen_calls[0].workspace == str((tmp_path / "workspace").resolve())
    snapshot = store.load_session(result.session.session_id)
    assert [message.role for message in snapshot.messages] == ["user", "assistant"]
    context_snapshot = store.load_latest_context(result.session.session_id)
    assert context_snapshot is not None
    assert context_snapshot.provider_context.messages == provider.messages
    assert len(context_snapshot.provider_context.messages) == 1


@pytest.mark.asyncio
async def test_runtime_persists_context_provider_message_with_provenance(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingProvider()
    context_providers = ContextProviderRegistry()
    context_providers.replace_owner(
        "ctx",
        (
            ContextProviderRegistration(
                plugin_id="dev.ouro.harness",
                provider_id="scaffold",
                generation_id="gen_harness",
                handler=lambda _call: ContextProviderResult(
                    fragments=[
                        ContextFragment(
                            text="durable harness context",
                            persistence="message",
                            semantic_tag="plugin-context",
                        )
                    ]
                ),
            ),
        ),
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
        context_providers=context_providers,
    )

    result = await runtime.run("Use durable context")

    snapshot = store.load_session(result.session.session_id)
    context_snapshot = store.load_latest_context(result.session.session_id)
    assert context_snapshot is not None
    scaffold = next(
        message
        for message in snapshot.messages
        if message.metadata.get("kind") == "context_provider"
    )
    assert scaffold.role == "user"
    assert scaffold.scaffold is True
    assert scaffold.semantic_tag == "plugin-context"
    assert scaffold.text_content == "durable harness context"
    assert scaffold.metadata == {
        "kind": "context_provider",
        "context_provider_delivery_key": (f"{result.run.run_id}:1:dev.ouro.harness:scaffold:0"),
        "plugin_id": "dev.ouro.harness",
        "contribution_id": "scaffold",
        "model_round": 1,
        "fragment_index": 0,
        "generation_id": "gen_harness",
    }
    assert (
        sum(
            message.role == "user"
            and message.text_content
            == "<plugin-context>\ndurable harness context\n</plugin-context>"
            for message in provider.messages
        )
        == 1
    )
    assert all(not hasattr(message, "message_id") for message in provider.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"text": "State B"},
        {"role": "system"},
        {"scaffold": False},
        {"semantic_tag": "updated-state"},
    ],
)
async def test_context_provider_retry_appends_changes_without_rewriting_history(
    tmp_path, change
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingProvider()
    model = ModelMetadata(model_id="capture", provider_id="capture")
    adapter = RuntimeAdapter(provider, model.model_copy(update={"context_window": 1}))
    initial = ContextFragment(text="State A", persistence="message", semantic_tag="test-state")
    fragment = initial
    context_providers = ContextProviderRegistry()
    context_providers.replace_owner(
        "ctx",
        (
            ContextProviderRegistration(
                plugin_id="dev.example.context",
                provider_id="state",
                handler=lambda _call: ContextProviderResult(fragments=[fragment]),
            ),
        ),
    )

    def create_runtime():
        return RuntimeService(
            store=store,
            adapter=adapter,
            tools=ToolRegistry(),
            context_providers=context_providers,
        )

    runtime = create_runtime()
    try:
        blocked = await runtime.run("Use current context")
        session_id = blocked.session.session_id
        assert blocked.run.status == "paused"
        before = store.load_session(session_id).messages
        await runtime.close()
        runtime = create_runtime()
        changed = initial.model_copy(update=change)
        for next_fragment, expected in (
            (initial, [initial]),
            (changed, [initial, changed]),
            (changed, [initial, changed]),
            (initial, [initial, changed, initial]),
        ):
            fragment = next_fragment
            retried = await runtime.continue_run(session_id=session_id)
            assert retried.run.run_id == blocked.run.run_id
            assert retried.run.status == "paused"
            assert retried.run.model_round_count == 0
            messages = store.load_session(session_id).messages
            assert messages[: len(before)] == before
            scaffolds = [m for m in messages if m.metadata.get("kind") == "context_provider"]
            assert len(scaffolds) == len(expected)
            for message, value in zip(scaffolds, expected, strict=True):
                assert message.text_content == value.text
                assert message.role == value.role
                assert message.scaffold == value.scaffold
                assert message.semantic_tag == value.semantic_tag
            before = messages

        adapter.model = model
        assert provider.calls == 0
        completed = await runtime.continue_run(session_id=session_id)
        assert completed.run.status == "completed"
        assert provider.calls == 1
        messages = store.load_session(session_id).messages
        assert messages[:-1] == before
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_context_provider_message_persistence_failure_blocks_model_request(
    tmp_path,
    monkeypatch,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingProvider()
    context_providers = ContextProviderRegistry()
    context_providers.replace_owner(
        "ctx",
        (
            ContextProviderRegistration(
                plugin_id="dev.ouro.harness",
                provider_id="required-scaffold",
                failure_mode="block",
                handler=lambda _call: ContextProviderResult(
                    fragments=[ContextFragment(text="must be durable", persistence="message")]
                ),
            ),
        ),
    )
    commit_session = store.commit_session

    def fail_scaffold(request):
        if any(
            step.message is not None and step.message.metadata.get("kind") == "context_provider"
            for step in request.steps
        ):
            raise PersistenceError("simulated scaffold persistence failure")
        return commit_session(request)

    monkeypatch.setattr(store, "commit_session", fail_scaffold)
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
        context_providers=context_providers,
    )

    result = await runtime.run("Do not lose the scaffold")

    assert result.run.status == "paused"
    assert result.run.metadata["pause"]["reason"] == "context_blocked"
    assert provider.messages == []
    snapshot = store.load_session(result.session.session_id)
    assert not any(
        message.metadata.get("kind") == "context_provider" for message in snapshot.messages
    )
    monkeypatch.setattr(store, "commit_session", commit_session)
    resumed = await runtime.continue_run(session_id=result.session.session_id)
    assert resumed.run.status == "completed"
    assert resumed.run.run_id == result.run.run_id
    assert "pause" not in resumed.run.metadata
    assert len(store.load_runs(result.session.session_id)) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_merges_context_builder_skills_before_provider_context(tmp_path) -> None:
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
    context_providers = ContextProviderRegistry()
    context_providers.replace_owner(
        "ctx",
        (
            ContextProviderRegistration(
                plugin_id="ctx",
                provider_id="plugin",
                handler=lambda _call: ContextProviderResult(
                    fragments=[ContextFragment(text="plugin context")]
                ),
            ),
        ),
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
        context_providers=context_providers,
        context_builder=context_builder,
    )

    await runtime.run("Use context")

    assert provider.system_prompt.startswith(DEFAULT_SYSTEM_PROMPT.strip())
    assert "<skills_instructions>" in provider.system_prompt
    assert "<name>pdf</name>" in provider.system_prompt
    scaffold_messages = [
        message.text_content for message in provider.messages if message.role == "user"
    ]
    assert scaffold_messages == ["plugin context", "Use context"]


@pytest.mark.asyncio
async def test_required_context_provider_failure_pauses_before_model_request(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    provider = CapturingProvider()
    context_providers = ContextProviderRegistry()

    def fail(_call):
        raise RuntimeError("required state unavailable")

    context_providers.replace_owner(
        "required",
        (
            ContextProviderRegistration(
                plugin_id="dev.ouro.required",
                provider_id="state",
                handler=fail,
                failure_mode="block",
            ),
        ),
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="capture", provider_id="capture"),
        ),
        tools=_tools(),
        context_providers=context_providers,
    )

    result = await runtime.run("Use required context")
    events = recorded_events(result.session.session_id)

    assert result.run.status == "paused"
    assert result.run.metadata["pause"]["reason"] == "context_blocked"
    assert provider.messages == []
    assert any(event.event_type == "context_provider_failed" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["files", "sqlite"])
async def test_turn_policy_continues_inside_same_run_before_completion(
    tmp_path, backend, monkeypatch
) -> None:
    store = (
        LocalSessionStore(tmp_path)
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "sessions.db")
    )
    store.initialize()
    provider = FauxProvider(
        event_batches=[
            [
                ProviderStreamEvent(type="text_delta", text="candidate"),
                ProviderStreamEvent(type="done"),
            ],
            [
                ProviderStreamEvent(type="text_delta", text="final"),
                ProviderStreamEvent(type="done"),
            ],
        ]
    )
    evaluations = 0

    def completion_policy(_candidate):
        nonlocal evaluations
        evaluations += 1
        if evaluations == 1:
            return TurnPolicyDecision(
                action="continue",
                feedback="Use a tool or provide a complete delivery.",
            )
        return TurnPolicyDecision()

    turn_policies = TurnPolicyRegistry()
    turn_policies.replace_owner(
        "policy",
        (
            TurnPolicyRegistration(
                plugin_id="dev.ouro.workflow",
                policy_id="completion",
                handler=completion_policy,
            ),
        ),
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
        turn_policies=turn_policies,
    )

    commit = store.commit_session
    feedback_commits = []

    def capture_commit(request):
        if any(
            step.message and step.message.semantic_tag == "turn-policy-feedback"
            for step in request.steps
        ):
            feedback_commits.append(request)
            assert [event.event_type for event in request.events] == ["turn_policy_continued"]
        return commit(request)

    async def drop_notifications(_events):
        pass

    monkeypatch.setattr(store, "commit_session", capture_commit)
    monkeypatch.setattr(runtime.event_recorder, "publish_committed", drop_notifications)
    result = await runtime.run("finish the workflow")
    assert len(feedback_commits) == 1
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "completed"
    assert result.final_text == "final"
    assert evaluations == 2
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "system",
        "assistant",
    ]
    assert snapshot.messages[1].text_content == "candidate"
    assert snapshot.messages[2].metadata["session_input_source"] == "turn_policy"
    # Notifications were deliberately dropped; continuation is proven by the
    # persisted feedback and the two responses in this same Run above.


@pytest.mark.asyncio
async def test_turn_policy_failure_pauses_instead_of_silently_completing(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    turn_policies = TurnPolicyRegistry()

    def fail(_candidate):
        raise RuntimeError("token=policy-secret")

    turn_policies.replace_owner(
        "policy",
        (
            TurnPolicyRegistration(
                plugin_id="dev.ouro.workflow",
                policy_id="completion",
                handler=fail,
            ),
        ),
    )
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(type="text_delta", text="candidate"),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
        turn_policies=turn_policies,
    )

    result = await runtime.run("finish")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "paused"
    assert result.run.metadata["pause"] == {
        "kind": "turn_policy",
        "reason": "turn_policy_blocked",
        "next_model_round": 2,
    }
    failure = next(
        event
        for event in recorded_events(snapshot.session.session_id)
        if event.event_type == "turn_policy_failed"
    )
    assert failure.payload["message"] == "token=***"
    turn_policies.replace_owner(
        "policy",
        (
            TurnPolicyRegistration(
                plugin_id="dev.ouro.workflow",
                policy_id="completion",
                handler=lambda _candidate: TurnPolicyDecision(),
            ),
        ),
    )
    resumed = await runtime.continue_run(session_id=result.session.session_id)
    assert resumed.run.status == "completed"
    assert resumed.run.run_id == result.run.run_id
    assert len(store.load_runs(result.session.session_id)) == 1
    await runtime.close()


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
    assert recorded_events(snapshot.session.session_id)[-1].event_type == "run_failed"


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
                    ProviderStreamEvent.from_tool_call(
                        {
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
    assert "approval_requested" in [
        event.event_type for event in recorded_events(snapshot.session.session_id)
    ]
    index = store.read_catalog()
    assert index.sessions[0].status == "waiting_approval"


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
                    ProviderStreamEvent.from_tool_call(
                        {
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
    await runtime.run(
        "Guide the active run",
        session_id=pending.session.session_id,
        queue_mode="steer",
    )
    snapshot = store.load_session(pending.session.session_id)

    assert second.run.run_id == pending.run.run_id
    assert second.run.status == "waiting_approval"
    assert [run.status for run in snapshot.runs] == ["waiting_approval"]
    assert [message.text_content for message in snapshot.messages] == ["Write file", ""]
    queued = store.load_input(snapshot.session.session_id, snapshot.session.pending_input_ids[0])
    assert queued is not None and queued.item.prompt == "Another prompt"
    assert [
        (item.prompt, item.mode) for item in runtime.pending_user_inputs(pending.session.session_id)
    ] == [
        ("Another prompt", "follow_up"),
        ("Guide the active run", "steer"),
    ]


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

    async with running_runtime_task(runtime, runtime.run("First prompt")) as first_task:
        await provider.first_started.wait()
        session = store.list_sessions()[0]

        queued = await runtime.run("Second prompt", session_id=session.session_id)
        queued_snapshot = store.load_session(session.session_id)
        provider.release_first.set()
        first = await first_task
        snapshot = store.load_session(session.session_id)

        assert queued.run.run_id == session.last_run_id
        assert len(queued_snapshot.session.pending_input_ids) == 1
        assert first.final_text == "first"
        assert provider.calls == 2
        assert not snapshot.session.pending_input_ids
        assert [run.status for run in snapshot.runs] == ["completed", "completed"]
        assert [message.text_content for message in snapshot.messages] == [
            "First prompt",
            "first",
            "Second prompt",
            "second",
        ]
        queued = store.load_input(
            queued_snapshot.session.session_id, queued_snapshot.session.pending_input_ids[0]
        )
        assert queued is not None and queued.item.prompt == "Second prompt"
        assert runtime.pending_user_inputs(session.session_id) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ("files", "sqlite"))
async def test_runtime_cancels_pending_user_input_before_follow_up_consumption(
    tmp_path,
    backend: str,
) -> None:
    store = (
        LocalSessionStore(tmp_path / "sessions")
        if backend == "files"
        else SQLiteSessionStore(tmp_path / "ouro.db")
    )
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

    async with running_runtime_task(runtime, runtime.run("First prompt")) as first_task:
        await provider.first_started.wait()
        session = store.list_sessions()[0]
        await runtime.run("Return this draft", session_id=session.session_id)
        queued = runtime.pending_user_inputs(session.session_id)[0]

        cancelled = await runtime.cancel_pending_user_input(session.session_id, queued.queue_id)
        provider.release_first.set()
        first = await first_task
        snapshot = store.load_session(session.session_id)

        assert cancelled == queued
        assert first.run.status == "completed"
        assert provider.calls == 1
        assert runtime.pending_user_inputs(session.session_id) == ()
        assert [message.text_content for message in snapshot.messages] == ["First prompt", "first"]
        cancelled_event = next(
            event
            for event in recorded_events(snapshot.session.session_id)
            if event.event_type == "queued_input_cancelled"
        )
        assert cancelled_event.payload == {
            "queue_id": queued.queue_id,
            "mode": "follow_up",
            "source": "user",
        }


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
                            type="reasoning_delta",
                            reasoning_content="I should read the file.",
                        ),
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "call_1",
                                "name": "Read",
                                "arguments": {"path": str(target)},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(
                            type="reasoning_delta",
                            reasoning_content="I should summarize the result.",
                        ),
                        ProviderStreamEvent(type="text_delta", text="I read the notes."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux", endpoint_id="chat"),
        ),
        tools=_tools(),
        default_workspace=tmp_path,
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
    assert snapshot.messages[1].tool_calls[0].id == "call_1"
    assert snapshot.messages[2].tool_result is not None
    assert snapshot.messages[2].tool_result.tool_call_id == "call_1"
    assert snapshot.messages[2].text_content == "1\timportant notes"
    assistant_messages = [message for message in snapshot.messages if message.role == "assistant"]
    assert [message.reasoning_content for message in assistant_messages] == [
        "I should read the file.",
        "I should summarize the result.",
    ]
    assert all(message.model_response is not None for message in assistant_messages)
    endpoints = [
        message.model_response.endpoint_id
        for message in assistant_messages
        if message.model_response is not None
    ]
    assert endpoints == [
        "chat",
        "chat",
    ]
    assert all(
        message.model_response is not None
        and message.model_response.reasoning_ended_at is not None
        and message.model_response.started_at
        <= message.model_response.reasoning_ended_at
        <= message.model_response.ended_at
        for message in assistant_messages
    )
    assert [event.event_type for event in recorded_events(snapshot.session.session_id)].count(
        "model_request_started"
    ) == 2
    assert result.run.model_round_count == 2


@pytest.mark.asyncio
async def test_runtime_records_provider_usage(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        usage_service=UsageService(LocalUsageLedger(tmp_path / "billing")),
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

    assert result.usage["input_tokens"] == 3
    assert result.usage["output_tokens"] == 2
    assert runtime.usage.run_usage(result.session.session_id, result.run.run_id).input_tokens == 3
    assert "usage" not in snapshot.runs[0].model_dump()
    assert "model_message_completed" in [
        event.event_type for event in recorded_events(snapshot.session.session_id)
    ]
    context_status = runtime.sessions.context_status(result.session.session_id)
    assert context_status is not None
    assert context_status.metrics.estimated_input_tokens > 0
    assert context_status.provider_reported_input_tokens == 3
    assert context_status.completed_at is not None


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
                        ProviderStreamEvent.from_tool_call(
                            {
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
        default_workspace=tmp_path,
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
    assert snapshot.messages[1].tool_calls[0].name == "Write"
    approval_resolved = next(
        event
        for event in recorded_events(snapshot.session.session_id)
        if event.event_type == "approval_resolved"
    )
    assert approval_resolved.payload["tool_call_id"] == "call_1"
    assert approval_resolved.payload["tool_call_status"] == "ready"
    assert "run_completed" in [
        event.event_type for event in recorded_events(snapshot.session.session_id)
    ]


@pytest.mark.asyncio
async def test_stale_approval_snapshot_cannot_reexecute_completed_tool(
    monkeypatch,
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "approve-once.txt"
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "call_once",
                                "name": "Write",
                                "arguments": {"path": str(target), "content": "once"},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="done"),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
        default_workspace=tmp_path,
    )
    pending = await runtime.run("Write once")
    stale = store.load_session(pending.session.session_id)
    await runtime.approve_tool(
        session_id=pending.session.session_id,
        run_id=pending.run.run_id,
        approved=True,
    )
    original_load = store.load_session
    load_count = 0

    def stale_then_current(session_id):
        nonlocal load_count
        load_count += 1
        if load_count == 1:
            return stale
        return original_load(session_id)

    monkeypatch.setattr(store, "load_session", stale_then_current)
    with pytest.raises(ValueError, match="not waiting for approval: completed"):
        await runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approved=True,
        )

    current = original_load(pending.session.session_id)
    assert target.read_text(encoding="utf-8") == "once"
    assert len([message for message in current.messages if message.role == "tool_result"]) == 1
    assert (
        len(
            [
                event
                for event in recorded_events(current.session.session_id)
                if event.event_type == "tool_call_started"
            ]
        )
        == 1
    )


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
                    ProviderStreamEvent.from_tool_call(
                        {
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
    assert "run_aborted" in [
        event.event_type for event in recorded_events(snapshot.session.session_id)
    ]


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
                        ProviderStreamEvent.from_tool_call(
                            {
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
        "tool_result",
        "user",
        "assistant",
    ]
    aborted_result = snapshot.messages[2]
    assert aborted_result.tool_result is not None
    assert aborted_result.tool_result.tool_call_id == "call_1"
    assert aborted_result.tool_result.is_error is True


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

    prefix_seen = asyncio.Event()
    runtime.add_event_sink(
        lambda event: prefix_seen.set() if event.event_type == "model_text_delta" else None
    )
    async with running_runtime_task(runtime, runtime.run("Wait for abort")) as run_task:
        session = await _wait_for_active_session(store)
        await asyncio.wait_for(prefix_seen.wait(), timeout=30)

        await runtime.abort_run(
            session_id=session.session_id,
            run_id=session.last_run_id,
        )
        result = await run_task
        snapshot = store.load_session(session.session_id)

        assert result.run.status == "aborted"
        assert result.session.status == "idle"
        assert result.final_text == "begin"
        assert provider.closed is True
        assert "run_aborted" in [
            event.event_type for event in recorded_events(snapshot.session.session_id)
        ]
        assert "run_completed" not in [
            event.event_type for event in recorded_events(snapshot.session.session_id)
        ]


@pytest.mark.asyncio
async def test_runtime_recovery_materializes_partial_output_from_current_draft(tmp_path) -> None:
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

    prefix_seen = asyncio.Event()
    runtime.add_event_sink(
        lambda event: prefix_seen.set() if event.event_type == "model_text_delta" else None
    )
    async with running_runtime_task(runtime, runtime.run("Wait for crash")) as run_task:
        session = await _wait_for_active_session(store)
        await asyncio.wait_for(prefix_seen.wait(), timeout=30)

        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

        recovered = runtime.session_recovery.recover(session.session_id)

        assert recovered.runs[0].status == "recoverable"
        assert recovered.session.status == "idle"
        assert [message.role for message in recovered.messages] == ["user", "assistant"]
        assert recovered.messages[-1].text_content == "begin"
        assert recovered.messages[-1].metadata["recovered_from_draft"] is True
        assert recovered.session.message_count == 2
        assert recovered.session.last_message_id == recovered.messages[-1].message_id


@pytest.mark.asyncio
async def test_stream_deltas_do_not_rewrite_session_or_run_snapshots(
    monkeypatch,
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    stream_events = [
        ProviderStreamEvent(type="reasoning_delta", reasoning_content=f"r{index}")
        for index in range(30)
    ]
    stream_events.extend(
        ProviderStreamEvent(type="text_delta", text=f"t{index}") for index in range(30)
    )
    stream_events.append(ProviderStreamEvent(type="done"))
    stream_write_counts = []

    class ObservedProvider(FauxProvider):
        async def stream(self, **kwargs):
            async for event in super().stream(**kwargs):
                if event.type in {"reasoning_delta", "text_delta"}:
                    stream_write_counts.append((session_writes, run_writes, commits))
                yield event

    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            ObservedProvider(stream_events),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )
    original_save_session = store.save_session
    original_save_run = store.save_run
    original_load_session = store.load_session
    original_commit = store.commit_session
    session_writes = 0
    run_writes = 0
    aggregate_loads = 0
    commits = 0

    def count_session_write(session) -> None:
        nonlocal session_writes
        session_writes += 1
        original_save_session(session)

    def count_run_write(run) -> None:
        nonlocal run_writes
        run_writes += 1
        original_save_run(run)

    def count_aggregate_load(session_id):
        nonlocal aggregate_loads
        aggregate_loads += 1
        return original_load_session(session_id)

    def count_commit(request):
        nonlocal commits
        commits += 1
        return original_commit(request)

    monkeypatch.setattr(store, "save_session", count_session_write)
    monkeypatch.setattr(store, "save_run", count_run_write)
    monkeypatch.setattr(store, "load_session", count_aggregate_load)
    monkeypatch.setattr(store, "commit_session", count_commit)

    result = await runtime.run("burst")

    assert result.run.status == "completed"
    assert result.final_text == "".join(f"t{index}" for index in range(30))
    assert len(stream_write_counts) == 60
    assert len(set(stream_write_counts)) == 1
    assert aggregate_loads <= 10


@pytest.mark.asyncio
async def test_runtime_aborts_active_approved_command_tool(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    shell = detect_command_shell()
    assert shell is not None
    command = f'"{sys.executable}" -c "import threading; threading.Event().wait()"'
    if shell.kind in ("powershell", "windows_powershell"):
        command = f"& {command}"
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent.from_tool_call(
                        {
                            "id": "shell_1",
                            "name": shell.tool_name,
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

    async with running_runtime_task(
        runtime,
        runtime.approve_tool(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
            approved=True,
        ),
    ) as approval_task:
        await _wait_for_event(store, pending.session.session_id, "tool_call_started")

        await runtime.abort_run(
            session_id=pending.session.session_id,
            run_id=pending.run.run_id,
        )
        result = await approval_task
        snapshot = store.load_session(pending.session.session_id)

        assert result.run.status == "aborted"
        assert result.session.status == "idle"
        assert "run_aborted" in [
            event.event_type for event in recorded_events(snapshot.session.session_id)
        ]
        assert not any(message.role == "tool_result" for message in snapshot.messages)
        completed = next(
            event
            for event in recorded_events(snapshot.session.session_id)
            if event.event_type == "tool_call_completed"
        )
        assert completed.payload["status"] == "unknown_outcome"
        completed_event = next(
            event
            for event in recorded_events(snapshot.session.session_id)
            if event.event_type == "tool_call_completed"
        )
        assert snapshot.runs[0].active_tool_batch is not None
        assert (
            completed_event.payload["status"]
            == load_tool_batch(store, snapshot.runs[0]).calls[0].status
        )
        assert snapshot.session.message_count == len(snapshot.messages)
        assert snapshot.runs[0].active_tool_batch is not None
        assert snapshot.runs[0].active_tool_batch.phase == "aborted"


@pytest.mark.asyncio
async def test_runtime_aborts_plugin_tool_through_execution_context(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    registry = _tools()
    started = asyncio.Event()

    async def wait_tool(invocation, context: ToolExecutionContext) -> ToolResult:
        started.set()
        await context.sleep(float("inf"))
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
                    ProviderStreamEvent.from_tool_call(
                        {
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
    try:
        async with asyncio.timeout(30):
            # Tool entry proves the Session exists; abort ends the wait rather
            # than racing a finite sleep or imposing a one-second I/O budget.
            await started.wait()
            session = store.list_sessions()[0]
            await runtime.abort_run(
                session_id=session.session_id,
                run_id=session.last_run_id,
            )
            result = await run_task
        snapshot = store.load_session(session.session_id)

        assert result.run.status == "aborted"
        assert result.session.status == "idle"
        assert "run_aborted" in [
            event.event_type for event in recorded_events(snapshot.session.session_id)
        ]
        assert not any(
            message.metadata.get("tool_name") == "WaitPlugin" for message in snapshot.messages
        )
    finally:
        if not run_task.done():
            run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_abort_owns_tool_cancellation_across_runtime_instances(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    registry = _tools()
    started = asyncio.Event()
    finalized = asyncio.Event()

    async def wait_tool(invocation, context) -> ToolResult:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="must not be committed",
        )

    registry.register(
        Tool(
            definition=ToolDefinition(
                name="HostCancelled",
                description="Requires host-owned task cancellation.",
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
                    ProviderStreamEvent.from_tool_call(
                        {
                            "id": "cancel_1",
                            "name": "HostCancelled",
                            "arguments": {},
                        }
                    ),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=registry,
    )
    aborter = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider([]),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=ToolRegistry(),
    )

    run_task = asyncio.create_task(runtime.run("Cancel the tool"))
    phase = "tool entry"
    try:
        async with asyncio.timeout(30):
            # The tool itself establishes readiness; disk I/O is not a latency contract.
            await started.wait()
            session = store.list_sessions()[0]
            phase = "cross-runtime abort"
            aborted = await aborter.abort_run(
                session_id=session.session_id, run_id=session.last_run_id
            )
            phase = "owner settlement"
            result = await run_task
        snapshot = store.load_session(session.session_id)
        event_types = [event.event_type for event in recorded_events(snapshot.session.session_id)]
        completed = next(
            event
            for event in recorded_events(snapshot.session.session_id)
            if event.event_type == "tool_call_completed"
        )

        assert finalized.is_set()
        assert aborted.run.status == "aborted"
        assert result.run.status == "aborted"
        assert completed.payload["status"] == "unknown_outcome"
        assert event_types.index("tool_call_completed") < event_types.index("tool_batch_aborted")
        assert event_types.index("tool_batch_aborted") < event_types.index("run_aborted")
        assert not any(message.role == "tool_result" for message in snapshot.messages)
    except TimeoutError:
        pytest.fail(f"Timed out during {phase}; owner={run_task!r}")
    finally:
        if not run_task.done():
            run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)
        await aborter.close()
        await runtime.close()


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
                        ProviderStreamEvent.from_tool_call(
                            {
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
    assert snapshot.messages[1].tool_calls[0].name == "Write"
    assert snapshot.messages[2].text_content == "not today"
    assert snapshot.messages[-1].text_content == "I will not write it."
    assert run_tool_results(store, resumed)[0].is_error is True


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
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "ask_1",
                                "name": "AskUser",
                                "arguments": {
                                    "questions": [
                                        {
                                            "id": "file",
                                            "header": "File",
                                            "question": "Which file?",
                                            "options": [
                                                {
                                                    "label": "notes.md (Recommended)",
                                                    "description": "Use the notes file.",
                                                },
                                                {
                                                    "label": "README.md",
                                                    "description": "Use the readme file.",
                                                },
                                            ],
                                        }
                                    ]
                                },
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
        answer={"answers": {"file": ["notes.md"]}},
    )
    snapshot = store.load_session(pending.session.session_id)

    assert pending.run.status == "waiting_user"
    assert resumed.run.status == "completed"
    assert resumed.final_text == "I will use notes.md."
    assert snapshot.messages[1].role == "assistant"
    assert snapshot.messages[1].tool_calls[0].name == "AskUser"
    assert snapshot.messages[2].role == "tool_result"
    assert snapshot.messages[2].text_content == '{"answers":{"file":["notes.md"]}}'
    assert snapshot.messages[-1].text_content == "I will use notes.md."
    ask_user_resolved = next(
        event
        for event in recorded_events(snapshot.session.session_id)
        if event.event_type == "ask_user_resolved"
    )
    assert ask_user_resolved.payload["tool_call_id"] == "ask_1"
    assert ask_user_resolved.payload["tool_call_status"] == "completed"


@pytest.mark.asyncio
async def test_runtime_drains_follow_up_after_rejected_approval_releases_run_lock(
    tmp_path,
) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    target = tmp_path / "rejected.txt"
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "write_1",
                                "name": "Write",
                                "arguments": {"path": str(target), "content": "blocked"},
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Write was rejected."),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Follow-up handled."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
        default_workspace=tmp_path,
    )
    waiting = await runtime.run("Write file")
    queued = await runtime.run("hi", session_id=waiting.session.session_id)

    resumed = await runtime.approve_tool(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        approved=False,
    )
    snapshot = store.load_session(waiting.session.session_id)

    assert queued.run.status == "waiting_approval"
    assert resumed.run.status == "completed"
    assert not target.exists()
    assert [run.status for run in snapshot.runs] == ["completed", "completed"]
    assert [message.text_content for message in snapshot.messages if message.role == "user"] == [
        "Write file",
        "hi",
    ]
    assert snapshot.messages[-1].text_content == "Follow-up handled."
    assert not snapshot.session.pending_input_ids


@pytest.mark.asyncio
async def test_runtime_drains_follow_up_after_user_answer_releases_run_lock(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {
                                "id": "ask_1",
                                "name": "AskUser",
                                "arguments": {
                                    "questions": [
                                        {
                                            "id": "file",
                                            "header": "File",
                                            "question": "Which file?",
                                            "options": [
                                                {
                                                    "label": "notes.md (Recommended)",
                                                    "description": "Use the notes file.",
                                                },
                                                {
                                                    "label": "README.md",
                                                    "description": "Use the readme file.",
                                                },
                                            ],
                                        }
                                    ]
                                },
                            },
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Answer accepted."),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Follow-up handled."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )
    waiting = await runtime.run("Need clarification")
    queued = await runtime.run("continue", session_id=waiting.session.session_id)

    resumed = await runtime.answer_user(
        session_id=waiting.session.session_id,
        run_id=waiting.run.run_id,
        answer={"answers": {"file": ["notes.md"]}},
    )
    snapshot = store.load_session(waiting.session.session_id)

    assert queued.run.status == "waiting_user"
    assert resumed.run.status == "completed"
    assert [run.status for run in snapshot.runs] == ["completed", "completed"]
    assert [message.text_content for message in snapshot.messages if message.role == "user"] == [
        "Need clarification",
        "continue",
    ]
    assert snapshot.messages[-1].text_content == "Follow-up handled."
    assert not snapshot.session.pending_input_ids


class ReasoningToolProvider:
    provider_id = "reasoning_tool"

    def __init__(self, read_path: str = "data.txt") -> None:
        self.calls = 0
        self.messages: list[list[ModelMessage]] = []
        self.system_prompts: list[str] = []
        self.read_path = read_path

    async def stream(
        self,
        *,
        model: ModelMetadata,
        system_prompt: str = "",
        messages,
        tools=None,
        request_context=None,
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
            yield ProviderStreamEvent.from_tool_call(
                {
                    "id": "call_1",
                    "name": "Read",
                    "arguments": {"path": self.read_path},
                },
            )
            yield ProviderStreamEvent(type="done")
            return
        yield ProviderStreamEvent(type="text_delta", text="Done")
        yield ProviderStreamEvent(type="done")

    async def check_connection(
        self, model: ModelMetadata, *, request_context=None
    ) -> ProviderCheckResult:
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )


@pytest.mark.asyncio
async def test_runtime_keeps_internal_reasoning_out_of_provider_context(tmp_path) -> None:
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
        default_workspace=tmp_path,
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
    assert not hasattr(assistant_message, "reasoning_content")
    assert assistant_message.provider_continuation is None
    assert assistant_message.tool_calls
    persisted_assistant = next(
        message for message in snapshot.messages if message.role == "assistant"
    )
    assert persisted_assistant.reasoning_content == "I should read the file."
    assert snapshot.messages[-1].text_content == "Done"


@pytest.mark.asyncio
async def test_runtime_releases_session_lock_when_startup_persistence_fails(tmp_path) -> None:
    class FailingStore(LocalSessionStore):
        def commit_session(self, request):
            if any(run.status == "running" for run in request.runs):
                raise PersistenceError("simulated start commit failure")
            return super().commit_session(request)

    store = FailingStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider([ProviderStreamEvent(type="text_delta", text="unused")]),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    with pytest.raises(PersistenceError, match="simulated start commit failure"):
        await runtime.run("hello")

    session = store.list_sessions()[0]
    assert store.is_session_locked(session.session_id) is False


@pytest.mark.asyncio
async def test_runtime_recovers_when_start_commit_response_is_lost(tmp_path) -> None:
    class LostStartResponseStore(LocalSessionStore):
        lose_start_response = True

        def commit_session(self, request):
            result = super().commit_session(request)
            if self.lose_start_response and request.runs and result.session.active_run_id:
                raise PersistenceError("simulated lost start response")
            return result

    store = LostStartResponseStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )
    try:
        with pytest.raises(PersistenceError, match="lost start response"):
            await runtime.run("hello")
        session = store.list_sessions()[0]
        assert store.is_session_locked(session.session_id) is False
        store.lose_start_response = False
        recovered = runtime.session_recovery.recover(session.session_id)
        assert recovered.session.status == "idle"
        assert recovered.session.active_run_id is None
        assert recovered.runs[0].status == "recoverable"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_runtime_isolates_failing_event_sink_and_records_diagnostic(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    sink_calls: list[str] = []

    def failing_sink(event) -> None:
        sink_calls.append(event.event_type)
        raise RuntimeError("renderer unavailable")

    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(type="text_delta", text="ok"),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
        event_sink=failing_sink,
    )

    result = await runtime.run("hello")
    snapshot = store.load_session(result.session.session_id)
    diagnostics = [
        event
        for event in recorded_events(snapshot.session.session_id)
        if event.event_type == "event_sink_failed"
    ]

    assert result.run.status == "completed"
    assert sink_calls == ["run_started"]
    assert len(diagnostics) == 1
    assert diagnostics[0].payload["source_event_type"] == "run_started"
    assert diagnostics[0].payload["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_runtime_automatically_compacts_and_persists_context_projection(tmp_path) -> None:
    class CompactingProvider(CapturingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.summary_calls = 0
            self.business_messages: list[MessageRecord] = []

        async def stream(
            self, *, model, system_prompt="", messages, tools=None, request_context=None
        ):
            del model, tools
            self.calls += 1
            if system_prompt == SUMMARIZATION_PROMPT:
                self.summary_calls += 1
                yield ProviderStreamEvent(
                    type="text_delta",
                    text=f"<summary>durable summary {self.summary_calls}</summary>",
                )
                yield ProviderStreamEvent(
                    type="done",
                    usage=Usage(input_tokens=20, output_tokens=5),
                )
                return
            self.business_messages = list(messages)
            yield ProviderStreamEvent(type="text_delta", text="ok")
            yield ProviderStreamEvent(type="done")

    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Context budget")
    store.create_session(session)
    for index in range(12):
        store.append_message(
            MessageRecord.text(
                session_id=session.session_id,
                role="user",
                text=f"old user {index} " + ("x" * 5000),
                sequence=index * 2 + 1,
            )
        )
        store.append_message(
            MessageRecord.text(
                session_id=session.session_id,
                role="assistant",
                text=f"old answer {index} " + ("y" * 5000),
                sequence=index * 2 + 2,
            )
        )
    provider = CompactingProvider()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(
                model_id="capture",
                provider_id="capture",
                context_window=32_000,
                max_output_tokens=4_000,
            ),
        ),
        tools=ToolRegistry(),
        system_prompt="",
    )

    result = await runtime.run("new", session_id=session.session_id)
    snapshot = store.load_session(session.session_id)

    assert result.run.status == "completed"
    assert provider.summary_calls >= 1
    assert snapshot.session.context_selection.compaction_id is not None
    assert snapshot.active_compaction is not None
    assert len(snapshot.messages) == 26
    assert provider.business_messages[0].role == "system"
    assert provider.business_messages[0].text_content.startswith(
        "<conversation-summary>\ndurable summary"
    )
    assert provider.business_messages[-1].text_content == "new"
    assert any(
        event.event_type == "compaction_completed"
        for event in recorded_events(snapshot.session.session_id)
    )


@pytest.mark.asyncio
async def test_runtime_returns_unknown_tool_error_to_model_and_continues(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                event_batches=[
                    [
                        ProviderStreamEvent.from_tool_call(
                            {"id": "call_1", "name": "Missing", "arguments": {}},
                        ),
                        ProviderStreamEvent(type="done"),
                    ],
                    [
                        ProviderStreamEvent(type="text_delta", text="Recovered."),
                        ProviderStreamEvent(type="done"),
                    ],
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    result = await runtime.run("use a missing tool")
    snapshot = store.load_session(result.session.session_id)

    assert result.run.status == "completed"
    assert result.final_text == "Recovered."
    assert run_tool_results(store, result)[0].structured["error_type"] == "UnknownTool"
    assert [message.role for message in snapshot.messages] == [
        "user",
        "assistant",
        "tool_result",
        "assistant",
    ]
    batch_started = next(
        event
        for event in recorded_events(snapshot.session.session_id)
        if event.event_type == "tool_batch_started"
    )
    assert batch_started.payload["tool_calls"] == [
        {
            "tool_call_id": "call_1",
            "tool_name": "Missing",
            "status": "failed",
            "source_index": 0,
        }
    ]


@pytest.mark.asyncio
async def test_runtime_reports_lock_race_as_typed_session_busy_error(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    session = SessionRecord(title="Busy")
    store.create_session(session)
    lock = store.acquire_session_lock(session.session_id, "run_other")
    assert lock is not None
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider([ProviderStreamEvent(type="done")]),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
    )

    try:
        with pytest.raises(SessionBusyError, match="already locked"):
            await runtime.run("hello", session_id=session.session_id)
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_runtime_persists_provider_retry_schedule_as_observable_event(tmp_path) -> None:
    store = LocalSessionStore(tmp_path)
    store.initialize()
    runtime = RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            FauxProvider(
                [
                    ProviderStreamEvent(
                        type="retry_scheduled",
                        retry=ProviderRetryNotice(
                            attempt=2,
                            max_attempts=3,
                            delay_seconds=0.5,
                            reason="provider request timed out",
                        ),
                    ),
                    ProviderStreamEvent(type="text_delta", text="Recovered."),
                    ProviderStreamEvent(type="done"),
                ]
            ),
            ModelMetadata(model_id="faux", provider_id="faux"),
        ),
        tools=_tools(),
        default_workspace=tmp_path,
    )

    result = await runtime.run("retry")
    retry = next(
        event for event in run_events(store, result) if event.event_type == "run_retry_scheduled"
    )

    assert result.final_text == "Recovered."
    assert retry.payload["attempt"] == 2
    assert retry.payload["max_attempts"] == 3
    assert retry.payload["round"] == 1
