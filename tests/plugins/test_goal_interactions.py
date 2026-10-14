"""Goal commands and policies exercised through real application boundaries."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import pytest

from ouro.application.runtime import RuntimeService
from ouro.application.runtime_environment import RuntimeEnvironment
from ouro.application.session_controls import SessionControlConflictError
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.config.compaction import CompactionConfig
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import CompactionArtifact, CompactionStrategyRegistry
from ouro.core.provider import ModelMetadata, ProviderCheckResult, ProviderStreamEvent, Usage
from ouro.core.state import SessionRecord
from ouro.core.tools import ToolCallRequest
from ouro.local_application import LocalApplication
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult

PLUGIN_ID = "dev.ouro.goal"


class ScriptedProvider:
    provider_id = "goal-interactions"

    def __init__(self):
        self.responses = deque()
        self.requests = []

    async def stream(self, *, model, messages, **kwargs):
        self.requests.append(list(messages))
        assert self.responses, "Goal started an unexpected model request"
        response = self.responses.popleft()
        if isinstance(response, ToolCallRequest):
            yield ProviderStreamEvent.from_tool_call(response)
        else:
            yield ProviderStreamEvent(type="text_delta", text=response)
        yield ProviderStreamEvent(type="done", usage=Usage(input_tokens=3, output_tokens=2))

    async def check_connection(self, model, **kwargs):
        return ProviderCheckResult(provider_id=self.provider_id, model_id=model.model_id, ok=True)


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


@pytest.mark.parametrize("mode", ["one_by_one", "all_at_once"])
@pytest.mark.parametrize(
    ("commands", "status", "tag"),
    [
        (["pause"], "paused", "goal-paused"),
        (["replace Revised objective"], "active", "goal-updated"),
        (["pause", "resume"], "active", "goal-resumed"),
        (["cancel"], None, "goal-cancelled"),
        (["clear"], None, "goal-cancelled"),
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
    assert len(pending) == 1
    assert pending[0].semantic_tag == tag
    assert "Current goal state for this request" not in pending[0].prompt
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
    assert "Status: complete" in g.provider.requests[-1][0].text_content
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
    assert "Status: active" in second[0].text_content
    assert "Check the result" in second[0].text_content
    assert "Goal: active" in second[-1].text_content
    assert "will be created" not in second[-1].text_content
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
    assert "The goal is stopped" in g.provider.requests[-1][0].text_content


@pytest.mark.parametrize("status", ["paused", "blocked", "budget_exhausted"])
async def test_stopped_goal_allows_discussion_and_reads_but_denies_changes(goal_app, status):
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
    assert (
        sum(
            "Current goal state for this request" in message.text_content
            for message in g.provider.requests[0]
        )
        == 1
    )
    used = goal["tokens_used"]

    g.provider.responses.extend(
        [
            ToolCallRequest(id="read", name="ReadInfo", arguments={}),
            ToolCallRequest(id="write", name="ChangeInfo", arguments={}),
            "The information is available; changes require resuming the goal.",
        ]
    )
    result = await g.say("Inspect the existing information and discuss the next step.")
    assert result.run.status == "completed"
    assert g.executed_tools == ["ReadInfo"]
    assert any(
        tool_result.is_error and "before making changes" in tool_result.text_content
        for tool_result in result.tool_results
    )
    assert (await g.goal())["status"] == status
    assert (await g.goal())["tokens_used"] == used
    assert g.pending() == []


async def test_compaction_preserves_current_goal_projection_without_persisting_it(goal_app):
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
    assert "Keep this exact objective" in messages[0].text_content
    assert "Status: paused" in messages[0].text_content
    assert any("<conversation-summary>" in message.text_content for message in messages)
    canonical = g.runtime.store.load_session(g.session.session_id).messages
    assert not any(message.semantic_tag == "goal-context" for message in canonical)


async def test_pause_during_model_request_stops_its_changes_and_refreshes_context(
    goal_app, monkeypatch
):
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
    await g.consume()
    assert g.executed_tools == []
    assert (await g.goal())["status"] == "paused"
    assert "Status: active" in g.provider.requests[0][0].text_content
    used = (await g.goal())["tokens_used"]
    await g.consume()
    assert "Status: paused" in g.provider.requests[1][0].text_content
    assert (await g.goal())["tokens_used"] == used
