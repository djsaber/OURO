from __future__ import annotations

import pytest

from ouro.application.inputs.skills import SkillInvocationRequest
from ouro.application.runtime.service import RuntimeService
from ouro.application.sessions.service import SessionService
from ouro.config.compaction import CompactionConfig
from ouro.core.adapter import RuntimeAdapter
from ouro.core.compaction import (
    CompactionArtifact,
    CompactionPolicy,
    CompactionRequest,
    CompactionStrategyRegistry,
)
from ouro.core.context import ContextBuilder
from ouro.core.messages import ModelMessage
from ouro.core.provider import ModelMetadata, ProviderStreamEvent
from ouro.core.run_budget import RunBudgetPolicy
from ouro.providers.anthropic_messages.codec import messages_to_anthropic_messages
from ouro.providers.openai_chat_completions.codec import messages_to_chat_completions
from ouro.providers.openai_responses.codec import responses_input
from ouro.sessions import LocalSessionStore
from ouro.skills import SkillLoader
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult
from tests.event_capture import recorded_events


class ToolLoopProvider:
    provider_id = "tool-loop"

    def __init__(self, rounds: int) -> None:
        self.rounds = rounds
        self.calls: list[list[ModelMessage]] = []
        self.system_prompts: list[str] = []

    async def stream(self, *, model, system_prompt="", messages, tools=None, request_context=None):
        self.calls.append(list(messages))
        self.system_prompts.append(system_prompt)
        completed = [
            int(message.tool_result.tool_call_id.removeprefix("read_"))
            for message in messages
            if message.tool_result
        ]
        next_read = max(completed, default=0) + 1
        if next_read <= self.rounds:
            yield ProviderStreamEvent.from_tool_call(
                {"id": f"read_{next_read}", "name": "ReadChunk", "arguments": {}}
            )
        else:
            yield ProviderStreamEvent(type="text_delta", text="finished")
        yield ProviderStreamEvent(type="done")


class RecordingSummary:
    strategy_id = "test.summary"
    version = "1"

    def __init__(self) -> None:
        self.requests: list[CompactionRequest] = []

    async def compact(self, request, *, signal=None):
        self.requests.append(request)
        return CompactionArtifact(summary=f"Progress summary {len(self.requests)}")


@pytest.mark.parametrize("skill_count", [0, 2])
@pytest.mark.parametrize("restart", [False, True])
async def test_long_run_compacts_between_tool_rounds_and_continues(tmp_path, skill_count, restart):
    skill_documents = []
    for index in range(skill_count):
        root = tmp_path / "skills" / f"skill-{index}"
        root.mkdir(parents=True)
        document = root / "SKILL.md"
        document.write_text(
            f"---\nname: skill-{index}\ndescription: Test instructions.\n---\n"
            f"Keep the exact active instruction {index}.\n",
            encoding="utf-8",
        )
        skill_documents.append(document)
    skills = SkillLoader(tmp_path / "skills").load_metadata() if skill_count else ()
    providers = []
    strategy = RecordingSummary()
    strategies = CompactionStrategyRegistry()
    strategies.register(strategy)
    executed = []

    def read_chunk(invocation, context):
        executed.append(invocation.tool_call_id)
        if len(executed) == 1:
            for document in skill_documents:
                document.unlink()
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=f"Result {invocation.tool_call_id}: " + "x" * 1200,
        )

    tools = ToolRegistry()
    tools.register(
        Tool(
            definition=ToolDefinition(
                name="ReadChunk", description="Read one chunk.", effect="read_only"
            ),
            handler=read_chunk,
        )
    )
    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()

    def create_runtime(*, pause=False):
        provider = ToolLoopProvider(rounds=10)
        providers.append(provider)
        return RuntimeService(
            store=LocalSessionStore(tmp_path / "sessions"),
            adapter=RuntimeAdapter(
                provider,
                ModelMetadata(
                    model_id="tool-loop",
                    provider_id=provider.provider_id,
                    context_window=3000,
                    max_output_tokens=500,
                ),
            ),
            tools=tools,
            skills=skills,
            system_prompt="Keep the base system instructions.",
            additional_system_prompt="Keep the supplementary instructions.",
            context_builder=ContextBuilder(
                skill_instructions="Keep the framework skill instructions."
            ),
            run_budget=RunBudgetPolicy(max_model_rounds=6) if pause else RunBudgetPolicy(),
            compaction_strategies=strategies,
            compaction_config=CompactionConfig(
                strategy_id=strategy.strategy_id,
                policy=CompactionPolicy(trigger_ratio=0.6, target_ratio=0.2),
            ),
        )

    runtime = create_runtime(pause=restart)
    try:
        result = await runtime.run(
            "Complete this long task",
            skill_invocations=tuple(
                SkillInvocationRequest(name=f"skill-{index}", arguments="Complete this long task")
                for index in range(skill_count)
            ),
        )

        if restart:
            assert result.run.status == "paused"
            assert strategy.requests
            run_id = result.run.run_id
            await runtime.close()
            # The soft budget only establishes the persisted interruption boundary.
            store.save_run(
                result.run.model_copy(
                    update={"metadata": {**result.run.metadata, "run_budget": {}}}
                )
            )
            runtime = create_runtime()
            result = await runtime.continue_run(session_id=result.session.session_id)
            assert result.run.run_id == run_id

        calls = [call for provider in providers for call in provider.calls]
        system_prompts = [prompt for provider in providers for prompt in provider.system_prompts]
        assert result.run.status == "completed"
        assert result.final_text == "finished"
        assert executed == [f"read_{index}" for index in range(1, 11)]
        assert len(calls) == 11
        assert len(set(system_prompts)) == 1
        for instruction in (
            "Keep the base system instructions.",
            "Keep the supplementary instructions.",
            "Keep the framework skill instructions.",
        ):
            assert instruction in system_prompts[-1]
            assert all(instruction in request.agent_system_prompt for request in strategy.requests)
            assert all(
                instruction not in message.text_content
                for request in strategy.requests
                for message in request.messages
            )
        assert len(strategy.requests) >= 2
        snapshot = store.load_session(result.session.session_id)
        assert [run.run_id for run in snapshot.runs] == [result.run.run_id]
        assert len(snapshot.messages) == 22
        assert all(message.run_id == result.run.run_id for message in snapshot.messages)
        assert all(request.run_id == result.run.run_id for request in strategy.requests)
        summarized_ids = [
            message.message_id for request in strategy.requests for message in request.messages
        ]
        assert len(summarized_ids) == len(set(summarized_ids))
        assert snapshot.messages[0].message_id in summarized_ids
        assert snapshot.messages[-2].message_id not in summarized_ids
        for index, record in enumerate(snapshot.compactions):
            assert record.trigger == "auto"
            assert record.estimated_tokens_before >= 1500
            assert record.estimated_tokens_after < record.estimated_tokens_before
            assert record.previous_compaction_id == (
                snapshot.compactions[index - 1].compaction_id if index else None
            )
            assert strategy.requests[index].previous_summary == (
                snapshot.compactions[index - 1].summary if index else None
            )
        for index, messages in enumerate(calls[1:], start=1):
            assert any(
                message.tool_result and message.tool_result.tool_call_id == f"read_{index}"
                for message in messages
            )
        for messages in calls:
            for index in range(skill_count):
                instructions = f"Keep the exact active instruction {index}."
                assert sum(message.text_content.count(instructions) for message in messages) == 1
                assert all(
                    message.role == "user"
                    for message in messages
                    if instructions in message.text_content
                )
        requests = [
            event
            for event in recorded_events(snapshot.session.session_id)
            if event.event_type == "model_request_started"
        ]
        assert [event.payload["round"] for event in requests] == list(range(1, 12))
        assert all(
            event.payload["context_metrics"]["estimated_input_tokens"] <= 2500 for event in requests
        )
        context = SessionService(store).context(result.session.session_id)
        assert context is not None
        assert context.provider_context.messages == calls[-1]

        # A fresh runtime and a new Run must reconstruct the same compacted prefix.
        await runtime.close()
        runtime = create_runtime()
        continued = await runtime.continue_run(
            session_id=result.session.session_id, allow_completed=True
        )
        assert continued.run.run_id != result.run.run_id
        assert len(store.load_session(result.session.session_id).compactions) == len(
            snapshot.compactions
        )
        await runtime.run("A separate task", session_id=result.session.session_id)
        calls.extend(providers[-1].calls)
        system_prompts.extend(providers[-1].system_prompts)
        _assert_compaction_prefixes(calls, system_prompts)
    finally:
        await runtime.close()


def _wire_views(messages, system_prompt):
    common = dict(system_prompt=system_prompt, provider_id="tool-loop", model_id="tool-loop")
    chat = messages_to_chat_completions(messages, supports_image_input=False, **common)
    responses = responses_input(messages, scope=(), **common)
    system, anthropic = messages_to_anthropic_messages(
        messages, supports_image_input=False, **common
    )
    return (
        (chat[0]["content"], chat[1:]),
        # Anthropic joins adjacent roles; appending content blocks preserves their prefix.
        (system, [(item["role"], block) for item in anthropic for block in item["content"]]),
        (responses["instructions"], responses["input"]),
    )


def _assert_compaction_prefixes(calls, system_prompts):
    previous = None
    previous_summary = None
    for messages, system_prompt in zip(calls, system_prompts, strict=True):
        summaries = [message for message in messages if message.role == "system"]
        assert len(summaries) <= 1
        summary = summaries[0].text_content if summaries else None
        views = _wire_views(messages, system_prompt)
        for system, _ in views:
            assert system == system_prompt + (f"\n\n{summary}" if summary else "")
        if previous is not None and summary == previous_summary:
            for (old_system, old_messages), (system, new_messages) in zip(
                previous, views, strict=True
            ):
                assert system == old_system
                assert new_messages[: len(old_messages)] == old_messages
        previous = views
        previous_summary = summary
