"""Goal commands and policies exercised through real application boundaries."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from itertools import pairwise

import pytest

from ouro.application.plugins.commands import consume_plugin_command, submit_plugin_command
from ouro.application.runtime.environment import RuntimeEnvironment
from ouro.application.runtime.service import RuntimeService
from ouro.application.session_controls import SessionControlConflictError
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.config.compaction import CompactionConfig
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import CompactionArtifact, CompactionStrategyRegistry
from ouro.core.errors import SessionBranchError
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent, Usage
from ouro.core.state import SessionRecord
from ouro.core.tools import ToolCallRequest
from ouro.local_application import LocalApplication
from ouro.providers.openai_responses.codec import responses_input
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from tests.application.helpers import wait_for_runtime_event

PLUGIN_ID = "dev.ouro.goal"


class ScriptedProvider:
    provider_id = "goal-interactions"

    def __init__(self):
        self.responses = deque()
        self.requests = []
        self.system_prompts = []

    async def stream(self, *, model, messages, **kwargs):
        self.requests.append(list(messages))
        self.system_prompts.append(kwargs["system_prompt"])
        assert self.responses, "Goal started an unexpected model request"
        response = self.responses.popleft()
        if isinstance(response, ToolCallRequest):
            yield ProviderStreamEvent.from_tool_call(response)
        else:
            yield ProviderStreamEvent(type="text_delta", text=response)
        yield ProviderStreamEvent(type="done", usage=Usage(input_tokens=3, output_tokens=2))

    async def check_connection(self, model, **kwargs):
        return ProviderCheckResult(provider_id=self.provider_id, model_id=model.model_id, ok=True)


def _assert_provider_input_prefix(provider):
    payloads = [
        responses_input(
            messages,
            system_prompt=system_prompt,
            provider_id=provider.provider_id,
            model_id="goal-test",
            scope=(),
        )
        for messages, system_prompt in zip(provider.requests, provider.system_prompts, strict=True)
    ]
    for previous, current in pairwise(payloads):
        assert current["instructions"] == previous["instructions"]
        assert current["input"][: len(previous["input"])] == previous["input"]


class GoalSummary:
    strategy_id = "test.goal-summary"
    version = "1"

    async def compact(self, request, *, signal=None):
        # Intentionally omit Goal state: the current state must come from its owner.
        return CompactionArtifact(summary="The user supplied background information.")


@dataclass
class GoalApplication:
    app: LocalApplication
    runtime: RuntimeService
    session: SessionRecord
    provider: ScriptedProvider
    executed_tools: list[str] = field(default_factory=list)

    async def command(self, argument):
        return await self.app.plugins.execute_command(
            f"{PLUGIN_ID}:goal", argument, session_id=self.session.session_id
        )

    async def goal(self):
        return (await self.command("status")).data["goal"]

    def pending(self):
        return self.runtime.queued_inputs.eligible_session(
            self.runtime.store.load_session_record(self.session.session_id)
        )

    async def consume(self):
        pending = self.pending()
        assert pending
        return await self.runtime.run(
            pending[0].prompt, session_id=self.session.session_id, _queued_inputs=pending
        )

    async def say(self, text):
        return await self.runtime.run(text, session_id=self.session.session_id)

    async def submit(self, argument):
        return await submit_plugin_command(
            self.app.plugins, self.runtime, "goal", argument, session_id=self.session.session_id
        )


@pytest.fixture(params=["files", "sqlite"])
async def goal_app(tmp_path, request):
    home = tmp_path / "home"
    store = (
        LocalSessionStore(home)
        if request.param == "files"
        else SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    )
    store.initialize()
    ledger = LocalUsageLedger(home)
    ledger.initialize()
    provider = ScriptedProvider()
    tools = ToolRegistry()
    executed = []

    def probe(invocation, context):
        executed.append(invocation.tool_name)
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content="Existing information." if invocation.tool_name == "ReadInfo" else "Changed.",
        )

    for name, effect in [("ReadInfo", "read_only"), ("ChangeInfo", "state_changing")]:
        tools.register(
            Tool(
                definition=ToolDefinition(
                    name=name, description=name, effect=effect, permission_mode="auto"
                ),
                handler=probe,
            )
        )
    summaries = CompactionStrategyRegistry()
    summaries.register(GoalSummary())
    runtime = RuntimeService(
        store=store,
        usage_service=UsageService(ledger),
        adapter=RuntimeAdapter(
            provider, ModelMetadata(model_id="goal-test", provider_id=provider.provider_id)
        ),
        tools=tools,
        compaction_strategies=summaries,
        compaction_config=CompactionConfig(strategy_id=GoalSummary.strategy_id),
        environment=RuntimeEnvironment.local(home),
    )
    app = LocalApplication.from_runtime(runtime, home=home)
    await app.start()
    await app.plugins.enable(PLUGIN_ID)
    session = runtime.create_session(approval_mode="auto")
    try:
        yield GoalApplication(app, runtime, session, provider, executed)
    finally:
        await app.close()


async def test_goal_creation_and_completion_preserve_the_provider_input_prefix(goal_app):
    g = goal_app
    g.provider.responses.append("Existing conversation.")
    await g.say("Background information.")
    await g.command("Check the result")
    g.provider.responses.extend(
        [
            ToolCallRequest(
                id="complete",
                name="UpdateGoal",
                arguments={"status": "complete", "report": "Checked the result."},
            ),
            "Goal complete.",
        ]
    )
    await g.consume()
    assert len(g.provider.requests) == 3
    _assert_provider_input_prefix(g.provider)
    messages = g.runtime.store.load_session(g.session.session_id).messages
    snapshots = [message for message in messages if message.semantic_tag == "goal-context"]
    assert snapshots
    assert all(message.role == "user" and message.scaffold for message in snapshots)
    assert "Status: complete" in snapshots[-1].text_content


@pytest.mark.parametrize("command", ["pause", "resume", "replace Updated objective", "cancel"])
async def test_goal_lifecycle_commands_preserve_the_provider_input_prefix(goal_app, command):
    g = goal_app
    await g.command("Original objective")
    await g.command("pause")
    g.provider.responses.append("The goal is paused.")
    await g.consume()
    if command == "pause":
        await g.command("resume")
    await g.command(command)
    goal = await g.goal()
    if goal is not None and goal["status"] == "active":
        g.provider.responses.append(
            ToolCallRequest(
                id="complete",
                name="UpdateGoal",
                arguments={"status": "complete", "report": "Verified the result."},
            )
        )
    g.provider.responses.append("Handled the latest operation.")
    if g.pending():
        await g.consume()
    else:
        await g.say("Handle the latest operation.")
    assert len(g.provider.requests) >= 2
    _assert_provider_input_prefix(g.provider)


async def test_goal_context_refreshes_when_resuming_a_context_blocked_round(goal_app):
    g = goal_app
    model = g.runtime.adapter.model
    g.runtime.adapter.model = model.model_copy(update={"context_window": 1000})
    await g.command("Inspect the project")
    blocked = await g.consume()
    assert blocked.run.status == "paused"
    assert blocked.run.metadata["pause"]["reason"] == "context_blocked"
    assert blocked.run.model_round_count == 0
    assert g.provider.requests == []
    assert (await g.goal())["status"] == "paused"
    before = g.runtime.store.load_session(g.session.session_id).messages
    assert "Status: active" in before[-1].text_content

    g.runtime.adapter.model = model
    g.provider.responses.append("The goal is paused.")
    resumed = await g.runtime.continue_run(session_id=g.session.session_id)
    assert resumed.run.run_id == blocked.run.run_id
    assert resumed.run.status == "completed"
    assert (await g.goal())["status"] == "paused"
    messages = g.runtime.store.load_session(g.session.session_id).messages
    assert messages[: len(before)] == before
    assert messages[-2].semantic_tag == "goal-context"
    assert messages[-2].role == "user"
    assert messages[-2].scaffold is True
    assert "Status: paused" in g.provider.requests[-1][-1].text_content


async def test_goal_branch_resets_persisted_context_and_business_state(goal_app):
    g = goal_app
    await g.command("Inspect the original project")
    await g.command("pause")
    g.provider.responses.append("Recorded initial progress.")
    result = await g.consume()
    assert result.run.status == "completed"
    source = g.runtime.store.load_session(g.session.session_id)
    context = next(m for m in source.messages if m.semantic_tag == "goal-context")
    branch = g.runtime.sessions.branch(g.session.session_id, source.messages[-1].message_id)
    branch_id = branch.session.session_id
    status = await g.app.plugins.execute_command(
        f"{PLUGIN_ID}:goal", "status", session_id=branch_id
    )
    assert status.data["goal"] is None
    messages = g.runtime.store.load_session(branch_id).messages
    assert not any(m.semantic_tag == "goal-context" for m in messages)
    assert any(m.text_content == "Inspect the original project" for m in messages)

    g.provider.responses.append("Discussing a different subject.")
    await g.runtime.run("Discuss a different subject.", session_id=branch_id)
    assert not any("<goal-context>" in m.text_content for m in g.provider.requests[-1])
    with pytest.raises(SessionBranchError, match="must reset"):
        g.runtime.sessions.branch(g.session.session_id, context.message_id)
    assert g.runtime.store.load_session(g.session.session_id).messages == source.messages


@pytest.mark.parametrize("resume", ["command", "continue", "message"])
async def test_pause_stops_stream_and_resume_keeps_existing_input(goal_app, monkeypatch, resume):
    g = goal_app
    started = asyncio.Event()
    stopped = asyncio.Event()
    original = g.provider.stream

    async def stream(**kwargs):
        if not started.is_set():
            started.set()
            try:
                yield ProviderStreamEvent(type="text_delta", text="Uncommitted response")
                await asyncio.Event().wait()
            finally:
                stopped.set()
        else:
            async for event in original(**kwargs):
                yield event

    monkeypatch.setattr(g.provider, "stream", stream)
    await g.command("Finish the retained work")
    task = asyncio.create_task(g.consume())
    try:
        await wait_for_runtime_event(started, task, description="goal model stream")
        before = g.runtime.store.load_session(g.session.session_id).messages
        async with asyncio.timeout(60):
            await g.submit("pause")
            assert stopped.is_set()
            result = await task
        assert result.run.status == "aborted"
        assert (await g.goal())["status"] == "paused"
        assert g.pending() == []
        assert g.runtime.store.load_session(g.session.session_id).messages == before

        resumed = await g.submit("resume") if resume == "command" else None
        assert g.pending() == []
        assert g.runtime.store.load_session(g.session.session_id).messages == before
        if resume == "command":
            g.provider.responses.append(
                ToolCallRequest(
                    id="finish",
                    name="UpdateGoal",
                    arguments={"status": "complete", "report": "Verified the retained work."},
                )
            )
        g.provider.responses.append("Done.")
        if resume == "command":
            results = [item async for item in consume_plugin_command(g.runtime, resumed)]
        elif resume == "continue":
            results = [await g.runtime.continue_run(session_id=g.session.session_id)]
        else:
            results = [await g.say("Discuss the current information.")]
        if resume != "message":
            assert results[-1].run.run_id == result.run.run_id
        assert results[-1].run.status == "completed", results[-1].run.error
        assert (await g.goal())["status"] == ("complete" if resume == "command" else "paused")
        messages = g.runtime.store.load_session(g.session.session_id).messages
        inputs = [m for m in messages if m.role == "user" and m.semantic_tag != "goal-context"]
        assert (inputs[:-1] if resume == "message" else inputs) == [
            m for m in before if m.role == "user" and m.semantic_tag != "goal-context"
        ]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("mode", ["one_by_one", "all_at_once"])
@pytest.mark.parametrize(
    ("commands", "status", "tag"),
    [
        (["pause"], "paused", "goal-create"),
        (["replace Revised objective"], "active", "goal-updated"),
        (["pause", "resume"], "active", "goal-create"),
        (["cancel"], None, "goal-cancelled"),
    ],
)
async def test_direct_creation_and_pending_lifecycle_coalescing(
    goal_app, mode, commands, status, tag
):
    g = goal_app
    g.runtime.queued_input_config = QueuedInputConfig(consumption_mode=mode)
    await g.command("--token-budget 100 Original objective")
    assert (await g.goal())["status"] == "active"
    assert g.provider.requests == []
    assert len(g.pending()) == 2
    for command in commands:
        await g.command(command)
    goal = await g.goal()
    assert (goal["status"] if goal else None) == status
    pending = g.pending()
    assert len(pending) == (2 if tag == "goal-create" else 1)
    assert pending[0].semantic_tag == tag
    assert "This status supersedes earlier goal notifications." not in pending[0].prompt
    assert "Call CreateGoal once" not in pending[0].prompt
    if tag in {"goal-updated", "goal-cancelled"}:
        assert "Original objective" not in pending[0].prompt
    assert g.provider.requests == []


async def test_plan_conflict_rejects_goal_and_its_messages_together(goal_app):
    g = goal_app
    await g.app.plugins.execute_command("dev.ouro.plan:plan", "on", session_id=g.session.session_id)
    before = g.runtime.store.load_harness(g.session.session_id)
    pending = g.pending()
    with pytest.raises(SessionControlConflictError):
        await g.command("Cannot replace Plan")
    assert await g.goal() is None
    assert g.runtime.store.load_harness(g.session.session_id) == before
    assert g.pending() == pending


async def test_failed_creation_commit_leaves_no_goal_or_messages(goal_app, monkeypatch):
    g = goal_app
    before = g.runtime.store.load_harness(g.session.session_id)

    def fail_commit(request):
        raise RuntimeError("injected commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(g.runtime.store, "commit_session", fail_commit)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            await g.command("Must be atomic")
    assert await g.goal() is None
    assert g.pending() == []
    assert g.runtime.store.load_harness(g.session.session_id) == before


async def test_creation_rejects_unfinished_goal_and_replaces_completed_record(goal_app):
    g = goal_app
    await g.command("--token-budget 20 First objective")
    before = await g.goal()
    pending = g.pending()
    rejected = await g.command("Second objective")
    assert rejected.data["error"] == "unfinished_goal_exists"
    assert await g.goal() == before
    assert g.pending() == pending
    g.provider.responses.extend(
        [
            ToolCallRequest(
                id="complete",
                name="UpdateGoal",
                arguments={"status": "complete", "report": "Verified the result."},
            ),
            "Complete.",
        ]
    )
    await g.consume()
    assert (await g.goal())["tokens_used"] == 5
    assert "Status: complete" in g.provider.requests[-1][-1].text_content
    await g.command("Second objective")
    current = await g.goal()
    assert current["status"] == "active"
    assert current["objective"] == "Second objective"
    assert current["tokens_used"] == 0
    assert current["token_budget"] is None
    assert current["created_at"] != before["created_at"]


@pytest.mark.parametrize("approval_mode", ["inherit", "approval", "deny"])
async def test_natural_language_creation_gets_context_and_preserves_request_usage(
    goal_app, approval_mode
):
    g = goal_app
    g.runtime.set_session_approval_mode(g.session.session_id, approval_mode)
    g.provider.responses.extend(
        [
            ToolCallRequest(
                id="create", name="CreateGoal", arguments={"objective": "Check the result"}
            ),
            ToolCallRequest(
                id="complete",
                name="UpdateGoal",
                arguments={"status": "complete", "report": "Checked the requested result."},
            ),
            "The result is checked.",
        ]
    )
    result = await g.say("Create a goal to check the result.")
    assert result.run.status == "completed"
    goal = await g.goal()
    assert goal["status"] == "complete"
    assert goal["tokens_used"] == 5
    assert len(g.provider.requests) == 3
    second = g.provider.requests[1]
    assert "Status: active" in second[-1].text_content
    assert "Check the result" in second[-1].text_content
    assert "Goal: active" in second[-2].text_content
    assert "will be created" not in second[-2].text_content
    messages = g.runtime.store.load_run_messages(g.session.session_id, result.run.run_id)
    assert not any(message.semantic_tag == "goal-create" for message in messages)


async def test_automatic_empty_work_pauses_and_user_information_does_not_resume(goal_app):
    g = goal_app
    g.provider.responses.extend(["Initial analysis.", "Still thinking."])
    await g.command("Investigate the issue")
    await g.consume()
    goal = await g.goal()
    assert goal["status"] == "paused"
    assert "without a tool call" in goal["report"]
    assert goal["tokens_used"] == 10
    assert len(g.provider.requests) == 2
    assert g.pending() == []

    g.provider.responses.append("Thanks for the additional information; the goal remains paused.")
    result = await g.say("Here is the missing context.")
    assert result.run.status == "completed"
    assert len(g.provider.requests) == 3
    assert (await g.goal())["status"] == "paused"
    assert (await g.goal())["tokens_used"] == 10
    assert "The goal is stopped" in g.provider.requests[-1][-1].text_content


@pytest.mark.parametrize("status", ["paused", "blocked", "budget_exhausted"])
async def test_stopped_goal_allows_normal_tools_without_resuming(goal_app, status):
    g = goal_app
    await g.command(
        "--token-budget 1 Read the information"
        if status == "budget_exhausted"
        else "Read the information"
    )
    if status == "paused":
        await g.command("pause")
    elif status == "blocked":
        g.provider.responses.append(
            ToolCallRequest(
                id="block",
                name="UpdateGoal",
                arguments={"status": "blocked", "report": "Three turns; awaiting missing data."},
            )
        )
    g.provider.responses.append("Current progress.")
    await g.consume()
    goal = await g.goal()
    assert goal["status"] == status
    assert sum("<goal-context>" in message.text_content for message in g.provider.requests[0]) == 1
    used = goal["tokens_used"]

    g.provider.responses.extend(
        [
            ToolCallRequest(id="read", name="ReadInfo", arguments={}),
            ToolCallRequest(id="write", name="ChangeInfo", arguments={}),
            "The requested independent change is done.",
        ]
    )
    result = await g.say("Inspect the information and make this independent change.")
    assert result.run.status == "completed"
    assert g.executed_tools == ["ReadInfo", "ChangeInfo"]
    assert not result.had_tool_error
    assert (await g.goal())["status"] == status
    assert (await g.goal())["tokens_used"] == used
    assert g.pending() == []


async def test_compaction_appends_current_goal_state_after_the_summary(goal_app):
    g = goal_app
    await g.command("Keep this exact objective")
    await g.command("pause")
    g.provider.responses.append("Paused.")
    await g.consume()
    g.provider.responses.append("Information received.")
    await g.say("Additional background information.")
    compacted = await g.runtime.compact(session_id=g.session.session_id)
    assert compacted.compaction is not None

    g.provider.responses.append("The goal remains paused.")
    await g.say("What is the current objective?")
    messages = g.provider.requests[-1]
    assert "Keep this exact objective" in messages[-1].text_content
    assert "Status: paused" in messages[-1].text_content
    assert any("<conversation-summary>" in message.text_content for message in messages)
    canonical = g.runtime.store.load_session(g.session.session_id).messages
    assert canonical[-2].semantic_tag == "goal-context"
    assert canonical[-2].role == "user"
    assert canonical[-2].scaffold is True


async def test_pause_during_model_request_discards_uncommitted_tool_calls(goal_app, monkeypatch):
    g = goal_app
    await g.command("Make the requested change")
    original_stream = g.provider.stream

    async def pause_during_first_request(**kwargs):
        async for event in original_stream(**kwargs):
            if len(g.provider.requests) == 1 and event.type == "tool_call_delta":
                await g.command("pause")
            yield event

    monkeypatch.setattr(g.provider, "stream", pause_during_first_request)
    g.provider.responses.extend(
        [
            ToolCallRequest(id="write", name="ChangeInfo", arguments={}),
            "The goal is paused; no changes were made.",
            "Paused.",
        ]
    )
    result = await g.consume()
    assert result.run.status == "aborted"
    assert g.executed_tools == []
    assert (await g.goal())["status"] == "paused"
    assert "Status: active" in g.provider.requests[0][-1].text_content
    assert len(g.provider.requests) == 1
    assert g.pending() == []


async def test_model_can_resume_stopped_goal_without_changing_its_budget(goal_app):
    g = goal_app
    await g.command("--token-budget 100 Finish this objective")
    await g.command("pause")
    g.provider.responses.append("I have retained the objective.")
    await g.consume()
    before = await g.goal()
    g.provider.responses.extend(
        [
            ToolCallRequest(
                id="resume",
                name="UpdateGoal",
                arguments={
                    "status": "active",
                    "report": "The missing information now allows progress.",
                },
            ),
            ToolCallRequest(
                id="finish",
                name="UpdateGoal",
                arguments={"status": "complete", "report": "The requested result was verified."},
            ),
            "Done.",
        ]
    )
    result = await g.say("Here is the missing information. Assess the next step.")
    assert result.run.status == "completed"
    assert not result.had_tool_error
    after = await g.goal()
    assert after["status"] == "complete"
    assert after["token_budget"] == before["token_budget"]
    assert after["created_at"] == before["created_at"]


async def test_resume_after_completed_discussion_adds_no_input(goal_app):
    g = goal_app
    await g.command("Keep working")
    await g.command("pause")
    g.provider.responses.append("Information received.")
    await g.consume()
    before = g.runtime.store.load_session(g.session.session_id).messages
    submission = await g.submit("resume")
    g.provider.responses.extend(
        [
            ToolCallRequest(
                id="finish",
                name="UpdateGoal",
                arguments={"status": "complete", "report": "Verified."},
            ),
            "Done.",
        ]
    )
    results = [item async for item in consume_plugin_command(g.runtime, submission)]
    assert results[-1].run.status == "completed"
    messages = g.runtime.store.load_session(g.session.session_id).messages
    assert [m for m in messages if m.role == "user" and m.semantic_tag != "goal-context"] == [
        m for m in before if m.role == "user" and m.semantic_tag != "goal-context"
    ]


@pytest.mark.parametrize("existing_dialogue", [False, True])
async def test_resume_before_first_execution_consumes_original_creation(
    goal_app, existing_dialogue
):
    g = goal_app
    if existing_dialogue:
        g.provider.responses.append("Earlier conversation.")
        await g.say("Background discussion.")
    await g.command("Start from the original objective")
    pending = g.pending()
    await g.submit("pause")
    submission = await g.submit("resume")
    assert g.pending() == pending
    g.provider.responses.extend(
        [
            ToolCallRequest(
                id="finish",
                name="UpdateGoal",
                arguments={"status": "complete", "report": "Verified."},
            ),
            "Done.",
        ]
    )
    results = [item async for item in consume_plugin_command(g.runtime, submission)]
    assert results[-1].run.status == "completed"
    assert g.pending() == []
    assert (await g.goal())["status"] == "complete"
    assert any(
        m.text_content == "Start from the original objective" for m in g.provider.requests[-1]
    )


async def test_cancel_revokes_a_delayed_resume_without_consuming_its_notification(goal_app):
    g = goal_app
    await g.command("Retain the requested objective")
    await g.submit("pause")
    resumed = await g.submit("resume")
    await g.command("cancel")
    pending = g.pending()
    assert [item.semantic_tag for item in pending] == ["goal-cancelled"]
    assert [result async for result in consume_plugin_command(g.runtime, resumed)] == []
    assert g.provider.requests == []
    assert g.pending() == pending
    assert g.runtime.store.load_session_record(g.session.session_id).continuation_request is None


async def test_pause_during_tool_execution_preserves_completed_results(goal_app, monkeypatch):
    g = goal_app
    started = asyncio.Event()
    stopped = asyncio.Event()
    executions = 0

    async def wait_tool(invocation, context):
        nonlocal executions
        executions += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(g.runtime.tools.get("ChangeInfo"), "handler", wait_tool)
    g.provider.responses.extend(
        [
            ToolCallRequest(id="read", name="ReadInfo", arguments={}),
            ToolCallRequest(id="change", name="ChangeInfo", arguments={}),
        ]
    )
    await g.command("Check then change the information")
    task = asyncio.create_task(g.consume())
    try:
        await wait_for_runtime_event(started, task, description="goal tool execution")
        async with asyncio.timeout(60):
            await g.submit("pause")
            assert stopped.is_set()
            paused = await task
        assert paused.run.status == "aborted"
        assert g.executed_tools == ["ReadInfo"]
        submission = await g.submit("resume")
        g.provider.responses.extend(
            [
                ToolCallRequest(
                    id="finish",
                    name="UpdateGoal",
                    arguments={"status": "complete", "report": "Verified the retained results."},
                ),
                "Done.",
            ]
        )
        results = [item async for item in consume_plugin_command(g.runtime, submission)]
        assert results[-1].run.status == "completed"
        assert executions == 1
        assert g.executed_tools == ["ReadInfo"]
        messages = g.runtime.store.load_session(g.session.session_id).messages
        receipts = [m.tool_result.tool_call_id for m in messages if m.tool_result]
        assert receipts.count("read") == receipts.count("change") == 1
        assert not any(m.semantic_tag in {"goal-paused", "goal-resumed"} for m in messages)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
