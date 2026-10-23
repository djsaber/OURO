from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from copy import deepcopy

import httpx
import pytest

from ouro.providers.http_transport import HttpxProviderHTTPTransport
from ouro.providers.openai_responses.errors import OpenAIResponsesError
from ouro.providers.openai_responses.stream import OpenAIResponsesStreamDecoder, responses_events
from tests.providers.responses_fixtures import (
    SCOPE,
    call,
    delta,
    item_event,
    message,
    reasoning,
    terminal,
)


def decoder():
    return OpenAIResponsesStreamDecoder(provider_id="provider", model_id="model", scope=SCOPE)


def decode(events):
    parser = decoder()
    result = []
    for event in events:
        result.extend(parser.feed(event))
    result.extend(parser.finish())
    return result


@pytest.mark.parametrize("mode", ["items", "snapshot", "both"])
def test_snapshot_and_item_completion_preserve_full_replay_and_emit_display_once(mode):
    output = [reasoning(), message(phase="final_answer")]
    events = [
        delta("response.reasoning_summary_text.delta", 0, "rs", "check", summary_index=0),
        delta("response.output_text.delta", 1, "msg", "hel", content_index=0),
    ]
    if mode != "snapshot":
        events.extend(item_event(i, item) for i, item in enumerate(output))
    events.append(terminal([] if mode == "items" else output))
    decoded = decode(events)
    assert "".join(event.text or "" for event in decoded) == "hello"
    assert "".join(event.reasoning_content or "" for event in decoded) == "checking"
    continuations = [event.continuation for event in decoded if event.type == "continuation"]
    assert len(continuations) == 1 and continuations[0].blocks[0]["items"] == output
    assert decoded[-1].type == "done" and decoded[-1].usage.output_tokens == 10
    assert sum(event.type == "done" for event in decoded) == 1


@pytest.mark.parametrize("mode", ["items", "snapshot", "both"])
def test_reasoning_text_and_summary_use_separate_coordinates_and_emit_once(mode):
    item = reasoning(content=[{"type": "reasoning_text", "text": "analysis"}])
    events = [
        delta("response.reasoning_text.delta", 0, "rs", "ana", content_index=0),
        {
            "type": "response.reasoning_text.done",
            "output_index": 0,
            "content_index": 0,
            "item_id": "rs",
            "text": "analysis",
        },
        delta("response.reasoning_summary_text.delta", 0, "rs", "check", summary_index=0),
    ]
    if mode != "snapshot":
        events.append(item_event(0, item))
    events.append(terminal([] if mode == "items" else [item]))
    decoded = decode(events)
    assert "".join(event.reasoning_content or "" for event in decoded) == "analysischecking"
    assert not any(event.type == "text_delta" for event in decoded)
    continuation = next(event.continuation for event in decoded if event.type == "continuation")
    assert continuation.blocks[0]["items"] == [item]


@pytest.mark.parametrize(
    "lane,coordinate",
    [("reasoning_text", "content_index"), ("reasoning_summary_text", "summary_index")],
)
def test_reasoning_display_does_not_require_text_to_be_repeated_in_replay_item(lane, coordinate):
    item = reasoning(summary=[])
    decoded = decode(
        [
            delta(f"response.{lane}.delta", 0, "rs", "visible", **{coordinate: 0}),
            terminal([item]),
        ]
    )
    assert "".join(event.reasoning_content or "" for event in decoded) == "visible"
    continuation = next(event.continuation for event in decoded if event.type == "continuation")
    assert continuation.blocks[0]["items"] == [item]


@pytest.mark.parametrize("summary", [None, []])
def test_final_reasoning_content_is_displayed_without_stream_deltas(summary):
    item = reasoning(summary=summary, content=[{"type": "reasoning_text", "text": "analysis"}])
    decoded = decode([terminal([item])])
    assert "".join(event.reasoning_content or "" for event in decoded) == "analysis"


@pytest.mark.parametrize(
    "content",
    [
        "invalid",
        [{"type": "reasoning_text", "text": 42}],
        [{"type": "output_text", "text": "wrong lane"}],
    ],
)
def test_malformed_reasoning_content_cannot_publish_success(content):
    with pytest.raises(OpenAIResponsesError):
        decode([terminal([reasoning(content=content)])])


def test_reasoning_text_conflicts_are_rejected():
    with pytest.raises(OpenAIResponsesError, match="conflicting_delta"):
        decode(
            [
                delta("response.reasoning_text.delta", 0, "rs", "first", content_index=0),
                terminal([reasoning(content=[{"type": "reasoning_text", "text": "different"}])]),
            ]
        )


def test_interleaved_tools_use_call_id_and_dense_tool_ordinals_without_duplicate_arguments():
    first, second = call(), call(2)
    output = [reasoning(), first, message(), second]
    decoded = decode(
        [
            item_event(0, output[0]),
            item_event(1, {**first, "status": "in_progress", "arguments": ""}, complete=False),
            item_event(2, output[2]),
            item_event(3, {**second, "status": "in_progress", "arguments": ""}, complete=False),
            delta("response.function_call_arguments.delta", 3, "item-2", '{"round":'),
            delta("response.function_call_arguments.delta", 1, "item-1", '{"round":1}'),
            delta("response.function_call_arguments.delta", 3, "item-2", "2}"),
            item_event(3, second),
            item_event(1, first),
            terminal(output),
        ]
    )
    fragments = [event.tool_call_delta for event in decoded if event.type == "tool_call_delta"]
    assert {fragment.index for fragment in fragments} == {0, 1}
    for index in (0, 1):
        parts = [f for f in fragments if f.index == index]
        assert "".join(f.id_delta for f in parts) == f"call-{index + 1}"
        assert "".join(f.name_delta for f in parts) == "echo"
        assert json.loads("".join(f.arguments_delta for f in parts)) == {"round": index + 1}
    assert decoded[-1].completion.finish_reason == "tool_call"


@pytest.mark.parametrize("mode", ["items", "snapshot", "both"])
@pytest.mark.parametrize("null_id", [False, True])
def test_function_call_optional_item_id_does_not_replace_required_call_id(mode, null_id):
    item = call()
    if null_id:
        item["id"] = None
    else:
        del item["id"]
    del item["status"]
    events = [] if mode == "snapshot" else [item_event(0, item)]
    events.append(terminal([] if mode == "items" else [item]))
    decoded = decode(events)
    fragments = [event.tool_call_delta for event in decoded if event.type == "tool_call_delta"]
    assert "".join(part.id_delta for part in fragments) == "call-1"
    assert "".join(part.name_delta for part in fragments) == "echo"
    assert json.loads("".join(part.arguments_delta for part in fragments)) == {"round": 1}
    assert decoded[-1].completion.finish_reason == "tool_call"
    continuation = next(event.continuation for event in decoded if event.type == "continuation")
    assert continuation.blocks[0]["items"] == [item]


@pytest.mark.parametrize("rich_event", [False, True])
def test_completed_snapshots_fill_nested_metadata_without_duplicating_text(rich_event):
    basic = message()
    del basic["content"][0]["annotations"]
    enriched = message(phase="final_answer", vendor_metadata={"trace": "fixture"})
    enriched["content"][0]["annotations"] = [{"type": "vendor_citation", "label": "source"}]
    enriched["content"][0]["logprobs"] = []
    pair = [enriched, basic] if rich_event else [basic, enriched]
    before = deepcopy(pair)
    decoded = decode([item_event(0, pair[0]), terminal([pair[1]])])
    assert pair == before
    assert "".join(event.text or "" for event in decoded) == "hello"
    continuation = next(event.continuation for event in decoded if event.type == "continuation")
    assert continuation.blocks[0]["items"] == [enriched]


@pytest.mark.parametrize(
    "changed",
    [
        message("different"),
        message(content=[{"type": "refusal", "refusal": "different"}]),
        message(content=[]),
        message(id="different"),
    ],
)
def test_completed_snapshot_differences_cannot_change_content_or_identity(changed):
    with pytest.raises(OpenAIResponsesError, match="conflicting_output_item"):
        decode([item_event(0, message()), terminal([changed])])


@pytest.mark.parametrize(
    "output",
    [
        [],
        [reasoning()],
        [message("")],
        [message(content=[{"type": "refusal", "refusal": "cannot comply"}])],
    ],
)
def test_empty_reasoning_only_and_refusal_responses_preserve_terminal_facts(output):
    result = decode([terminal(output)])
    assert result[-1].type == "done"
    assert result[-1].completion.has_reasoning == any(
        item["type"] == "reasoning" for item in output
    )
    refused = any(part["type"] == "refusal" for item in output for part in item.get("content", []))
    assert result[-1].completion.finish_reason == ("content_filter" if refused else "stop")
    assert "".join(event.text or "" for event in result) == (
        "cannot comply"
        if output and output[0].get("content") and output[0]["content"][0]["type"] == "refusal"
        else ""
    )


@pytest.mark.parametrize(
    "events,code",
    [
        ([], "missing_terminal"),
        ([item_event(0, message())], "missing_terminal"),
        ([terminal([]), terminal([])], "duplicate_terminal"),
        ([terminal([]), item_event(0, message())], "output_after_terminal"),
        ([{"type": "response.failed", "response": {"error": "private-secret"}}], "response_failed"),
        (
            [{"type": "response.incomplete", "response": {"output": [call()]}}],
            "response_incomplete",
        ),
        ([{"type": "error", "message": "private-secret"}], "response_failed"),
        ([terminal([], status="in_progress")], "invalid_terminal"),
        ([terminal([], error={"message": "private-secret"})], "invalid_terminal"),
        ([terminal([], incomplete_details={"reason": "max_output_tokens"})], "invalid_terminal"),
        ([terminal([call(arguments="{")])], "invalid_tool_arguments"),
        ([terminal([call(arguments="[]")])], "invalid_tool_arguments"),
        ([terminal([call(arguments='{"x": NaN}')])], "invalid_tool_arguments"),
        ([terminal([call(arguments='{"x": 1e999}')])], "invalid_tool_arguments"),
        ([terminal([call(arguments='{"x": 1, "x": 2}')])], "invalid_tool_arguments"),
        ([terminal([call(id=False)])], "invalid_output_item"),
        ([terminal([call(call_id=None)])], "invalid_output_item"),
        ([terminal([call(name="")])], "invalid_output_item"),
        ([terminal([message(id=None)])], "invalid_output_item"),
        (
            [item_event(0, call()), terminal([call(arguments='{"round":2}')])],
            "conflicting_output_item",
        ),
        ([terminal([call(), call(2, call_id="call-1")])], "duplicate_tool_call"),
        ([item_event(1, message()), terminal([])], "unfinished_output_item"),
        (
            [item_event(0, message(status="in_progress"), complete=False), terminal([])],
            "unfinished_output_item",
        ),
        ([item_event(0, message()), terminal([message("different")])], "conflicting_output_item"),
        ([terminal([{"type": "web_search_call", "id": "search"}])], "unsupported_output_item"),
        (
            [
                delta("response.output_text.delta", 0, "msg", "wrong", content_index=0),
                terminal([message()]),
            ],
            "conflicting_delta",
        ),
        (
            [
                delta("response.output_text.delta", 0, "msg", "x", content_index=1),
                terminal([message()]),
            ],
            "conflicting_output_item",
        ),
    ],
)
def test_malformed_or_unsuccessful_streams_cannot_publish_done_or_continuation(events, code):
    parser = decoder()
    seen = []
    with pytest.raises(OpenAIResponsesError) as failure:
        for event in events:
            seen.extend(parser.feed(event))
        seen.extend(parser.finish())
    assert failure.value.code == code
    assert "private-secret" not in str(failure.value)
    assert not any(event.type in {"done", "continuation"} for event in seen)


def test_incomplete_response_after_tool_fragments_does_not_complete_the_call():
    parser = decoder()
    fragments = parser.feed(item_event(0, {**call(), "status": "in_progress"}, complete=False))
    assert fragments and fragments[0].type == "tool_call_delta"
    with pytest.raises(OpenAIResponsesError, match="response_incomplete"):
        parser.feed({"type": "response.incomplete"})


def test_identical_done_events_only_fill_unseen_suffix_and_future_metadata_is_ignored():
    part_done = {
        "type": "response.output_text.done",
        "output_index": 0,
        "content_index": 0,
        "item_id": "msg",
        "text": "hello",
    }
    result = decode(
        [
            delta("response.output_text.delta", 0, "msg", "h", content_index=0),
            {"type": "response.future_metadata", "value": "unknown"},
            part_done,
            part_done,
            item_event(0, message()),
            terminal([message()]),
        ]
    )
    assert "".join(event.text or "" for event in result) == "hello"


@pytest.mark.parametrize(
    "extension",
    [
        {"type": "vendor.billing", "cost": "0.01"},
        {"type": "response.future_metadata", "value": "unknown"},
        {"type": "ping", "cost": "0"},
    ],
)
def test_extensions_at_any_stream_position_do_not_change_completion(extension):
    output = [message(), call()]
    events = [item_event(i, item) for i, item in enumerate(output)] + [terminal(output)]
    interleaved = [extension]
    for event in events:
        interleaved.extend([event, extension])
    assert decode(interleaved) == decode(events)
    with pytest.raises(OpenAIResponsesError, match="missing_terminal"):
        decode([extension, item_event(0, message()), extension])


@pytest.mark.parametrize(
    "kind",
    [
        "response.output_text.delta",
        "response.output_text.done",
        "response.refusal.delta",
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.done",
        "response.reasoning_summary_part.done",
        "response.output_text.annotation.added",
        "response.created",
        "response.in_progress",
        "response.failed",
        "response.incomplete",
        "error",
        "response.completed",
    ],
)
def test_extensions_after_terminal_do_not_hide_late_output_or_lifecycle_events(kind):
    parser = decoder()
    assert parser.feed(terminal([message()])) == []
    assert parser.feed({"type": "vendor.metadata"}) == []
    code = "duplicate_terminal" if kind == "response.completed" else "output_after_terminal"
    with pytest.raises(OpenAIResponsesError, match=code):
        parser.feed({"type": kind})


@pytest.mark.parametrize("invalid", [{}, {"type": None}, {"type": 42}])
def test_extensions_still_require_an_event_type_after_terminal(invalid):
    parser = decoder()
    parser.feed(terminal([]))
    with pytest.raises(OpenAIResponsesError, match="invalid_event"):
        parser.feed(invalid)


async def test_shared_http_sse_transport_drives_the_decoder():
    async def handler(request):
        events = [item_event(0, message()), terminal([]), {"type": "vendor.billing", "cost": "0"}]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(e) + "\n\n" for e in events),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HttpxProviderHTTPTransport(client)
        chunks = transport.stream_json(
            url="https://example.test/responses", headers={}, payload={}, timeout=5
        )
        events = [
            event
            async for event in responses_events(
                chunks, provider_id="provider", model_id="model", scope=SCOPE
            )
        ]
        assert events[-1].type == "done" and events[0].text == "hello"
        assert not client.is_closed


@pytest.mark.parametrize("cancel", [False, True])
async def test_cancel_and_early_consumer_exit_close_the_owned_stream(cancel):
    closed, entered = asyncio.Event(), asyncio.Event()

    async def chunks():
        try:
            if cancel:
                entered.set()
                await asyncio.Event().wait()
            yield item_event(0, message())
        finally:
            closed.set()

    async def consume():
        async with aclosing(
            responses_events(chunks(), provider_id="p", model_id="m", scope=SCOPE)
        ) as stream:
            async for event in stream:
                assert event.type == "text_delta"
                break

    task = asyncio.create_task(consume())
    if cancel:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert closed.is_set()
