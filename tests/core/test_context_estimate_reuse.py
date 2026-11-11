from __future__ import annotations

import gc
import weakref

import pytest

import ouro.core.context as contexts
from ouro.core.context_budget import ContextBudget
from ouro.core.context_providers import ContextFragment
from ouro.core.messages import MessageRecord, ProviderContinuationArtifact, TextContent
from ouro.core.tools import ToolCallRequest, ToolDefinition


def build(builder, messages, **kwargs):
    return builder.build(messages=messages, source_messages=messages, tools=[], **kwargs)


@pytest.mark.parametrize("count", [4, 128])
def test_default_estimates_reuse_retained_units_and_release_removed_content(count, monkeypatch):
    measured = []
    estimate = contexts._estimate_visible_message_tokens

    def count_estimates(message, estimator):
        measured.append(message.role)
        return estimate(message, estimator)

    monkeypatch.setattr(contexts, "_estimate_visible_message_tokens", count_estimates)
    builder = contexts.ContextBuilder()
    messages = [
        MessageRecord.text(session_id="session", role="user", text="body " * 8192)
        for _ in range(count)
    ]
    body = weakref.ref(messages[0].content[0])
    first = build(builder, messages)
    assert len(measured) == count
    added = MessageRecord.text(session_id="session", role="assistant", text="new")
    messages.append(added)
    second = build(builder, messages)
    assert len(measured) == count + 1
    added_cost = estimate(contexts.model_visible_message(added), contexts.estimate_text_tokens)
    assert second.metrics.estimated_input_tokens == (
        first.metrics.estimated_input_tokens + added_cost
    )
    # Only the retained current input survives; no history/body cache is held.
    third = build(builder, [added])
    assert len(measured) == count + 1
    assert third.metrics.estimated_input_tokens == added_cost
    assert len(builder._message_estimates) == 1
    del first, second, messages
    gc.collect()
    assert body() is None
    # Re-selection after eviction measures the restored immutable unit again.
    old = MessageRecord.text(session_id="session", role="user", text="selected again")
    build(builder, [old, added])
    assert len(measured) == count + 2


def test_default_reuse_keeps_dynamic_fragments_tools_and_budget_current():
    builder = contexts.ContextBuilder()
    source = MessageRecord.text(session_id="session", role="user", text="stable history")
    tool = ToolDefinition(name="Act", description="first")
    first = builder.build(
        messages=[source],
        source_messages=[source],
        tools=[tool],
        added_context=[ContextFragment(text="first dynamic")],
    )
    changed = dict(
        messages=[source],
        source_messages=[source],
        tools=[tool.model_copy(update={"description": "different capability " * 20})],
        added_context=[ContextFragment(text="second dynamic " * 20)],
        system_prompt="new instructions",
        budget=ContextBudget(context_window=10000),
    )
    reused = builder.build(**changed)
    fresh = contexts.ContextBuilder().build(**changed)
    assert reused == fresh
    assert reused.metrics != first.metrics
    assert "second dynamic" in reused.provider_context.messages[0].text_content
    assert reused.metrics.budget.context_window == 10000


def test_transformed_literals_and_session_identity_cannot_reuse_stale_costs():
    builder = contexts.ContextBuilder()
    source = MessageRecord.text(session_id="first", role="user", text="original")
    build(builder, [source])
    transformed = source.model_copy(update={"content": [TextContent(text="changed " * 100)]})
    arguments = dict(messages=[transformed], source_messages=[source], tools=[])
    assert builder.build(**arguments) == contexts.ContextBuilder().build(**arguments)
    other = source.model_copy(update={"session_id": "second", "content": transformed.content})
    assert build(builder, [other]) == build(contexts.ContextBuilder(), [other])


@pytest.mark.parametrize("removed", ["tool_calls", "provider_continuation", "reasoning_content"])
def test_default_reuse_distinguishes_projected_message_members(removed):
    builder = contexts.ContextBuilder()
    source = MessageRecord(
        session_id="session",
        role="assistant",
        reasoning_content="readable reasoning " * 1000 if removed == "reasoning_content" else "",
        tool_calls=[
            ToolCallRequest(id="call", name="Act", arguments={"text": "arguments " * 1000})
        ],
        provider_continuation=None
        if removed == "reasoning_content"
        else ProviderContinuationArtifact(
            provider_id="provider",
            protocol="protocol",
            model_id="model",
            blocks=({"opaque": "native data " * 1000},),
        ),
    )
    full = build(builder, [source])
    absent = {"tool_calls": [], "provider_continuation": None, "reasoning_content": ""}
    selected = source.model_copy(update={removed: absent[removed]})
    arguments = dict(messages=[selected], source_messages=[source], tools=[])
    projected = builder.build(**arguments)
    assert projected == contexts.ContextBuilder().build(**arguments)
    assert projected.metrics.estimated_input_tokens < full.metrics.estimated_input_tokens


def test_reasoning_uses_reference_estimates_without_remeasuring_retained_text(monkeypatch):
    measured = []
    estimate = contexts._estimate_visible_message_tokens

    def count(message, estimator):
        measured.append(message.reasoning_content)
        return estimate(message, estimator)

    monkeypatch.setattr(contexts, "_estimate_visible_message_tokens", count)
    builder = contexts.ContextBuilder()
    source = MessageRecord(
        session_id="session", role="assistant", reasoning_content="thought " * 4096
    )
    first = build(builder, [source])
    second = build(builder, [source])
    assert first == second
    assert len(measured) == 1
    assert source.reasoning_content not in next(iter(builder._message_estimates))


def test_custom_estimator_is_called_for_the_full_current_input_each_time():
    calls = []
    multiplier = 1

    def estimator(text):
        calls.append(text)
        return len(text) * multiplier

    builder = contexts.ContextBuilder(token_estimator=estimator)
    message = MessageRecord.text(session_id="session", role="user", text="custom input")
    first = build(builder, [message])
    measured = list(calls)
    calls.clear()
    multiplier = 2
    second = build(builder, [message])
    assert calls == measured
    assert second.metrics.estimated_input_tokens == 2 * first.metrics.estimated_input_tokens - 4
    assert not builder._message_estimates
