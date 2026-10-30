from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from dev.probes.codex_oauth import ProbeError
from dev.probes.codex_responses import RESPONSES_URL, request, verify_responses
from ouro.auth.contracts import CredentialGrant, CredentialIdentity


def grant():
    return CredentialGrant(
        secret_payload={"access_token": "access-secret", "refresh_token": "refresh-secret"},
        identity=CredentialIdentity(account_id="account-secret"),
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
    )


def message(text):
    return {
        "type": "message",
        "id": "message-secret",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def completed(output):
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "id": "response-secret",
            "output": output,
            "usage": {
                "input_tokens": 10,
                "output_tokens": 6,
                "total_tokens": 16,
                "input_tokens_details": {"cached_tokens": 2},
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        },
    }


def sse(events):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="".join("data: " + json.dumps(event) + "\n\n" for event in events),
    )


@pytest.mark.parametrize("output_mode", ["snapshot", "items", "both"])
async def test_four_requests_replay_complete_items_once_and_only_export_safe_facts(output_mode):
    bodies = []
    first_items = [
        {"type": "reasoning", "id": "reasoning-secret", "encrypted_content": "cipher-secret"},
        {
            "type": "function_call",
            "id": "item-1",
            "call_id": "call-1",
            "name": "ouro_probe_echo",
            "arguments": '{"round":1}',
        },
    ]
    second_items = [
        {
            "type": "function_call",
            "id": "item-2",
            "call_id": "call-2",
            "name": "ouro_probe_echo",
            "arguments": '{"round":2}',
        }
    ]
    outputs = iter(
        [
            [message("**OURO_OK**.")],
            first_items,
            second_items,
            [message("Verified: `OURO_TOOLS_OK`.\n")],
        ]
    )

    def handler(incoming):
        assert str(incoming.url) == RESPONSES_URL
        assert incoming.headers["authorization"] == "Bearer access-secret"
        assert incoming.headers["chatgpt-account-id"] == "account-secret"
        body = json.loads(incoming.content)
        bodies.append(body)
        assert body["store"] is False and body["stream"] is True
        assert "previous_response_id" not in body and "max_output_tokens" not in body
        assert body["include"] == ["reasoning.encrypted_content"]
        output = next(outputs)
        events = [{"type": "event-with-private-data"}]
        if output_mode != "snapshot":
            events.extend(item_done(index, item) for index, item in enumerate(output))
        events.append(completed(output if output_mode != "items" else []))
        return sse(events)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await verify_responses(client, grant(), model="test-model")
    assert len(bodies) == len(report) == 4
    for item in first_items:
        assert bodies[2]["input"].count(item) == bodies[3]["input"].count(item) == 1
    assert bodies[3]["input"].count(second_items[0]) == 1
    results = [item for item in bodies[3]["input"] if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in results] == ["call-1", "call-2"]
    assert [json.loads(item["output"]) for item in results] == [
        {"round": 1, "ok": True},
        {"round": 2, "ok": True},
    ]
    assert report[1]["encrypted_reasoning"] is True
    assert report[0]["usage"] == {
        "input_tokens": 10,
        "output_tokens": 6,
        "total_tokens": 16,
        "cached_tokens": 2,
        "reasoning_tokens": 3,
    }
    serialized = json.dumps(report)
    assert "secret" not in serialized and "private-data" not in serialized
    assert "OURO_OK" not in serialized and "call-1" not in serialized
    assert report[0]["event_counts"]["unknown"] == 1
    assert all(item["validated"] for item in report)


def item_done(index, item):
    return {"type": "response.output_item.done", "output_index": index, "item": item}


@pytest.mark.parametrize("terminal_output", [[], None])
async def test_streamed_reasoning_and_text_survive_empty_or_absent_terminal_output(terminal_output):
    reasoning = {
        "id": "reasoning-secret",
        "type": "reasoning",
        "summary": [],
        "encrypted_content": "cipher-secret",
    }
    text = message("OURO_OK")
    terminal = completed(terminal_output)
    if terminal_output is None:
        del terminal["response"]["output"]
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "reasoning-secret", "type": "reasoning"},
        },
        {"type": "response.output_text.delta", "output_index": 1, "delta": "OURO_"},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "OK"},
        item_done(1, text),
        item_done(0, reasoning),
        item_done(1, text),
        terminal,
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: sse(events))) as client:
        observation = await request(client, grant(), model="test", input_items=[])
    assert observation.output == [reasoning, text]
    assert observation.report()["text_characters"] == 7
    assert observation.encrypted_reasoning is True
    assert "secret" not in json.dumps(observation.report())


async def test_terminal_snapshot_completes_added_item_without_done_event():
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "message-secret", "type": "message", "content": []},
        },
        completed([message("OURO_OK")]),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: sse(events))) as client:
        observation = await request(client, grant(), model="test", input_items=[])
    assert observation.output == [message("OURO_OK")]


async def test_snapshot_preserves_the_selected_complete_item():
    item = message("OURO_OK")
    events = [item_done(0, item), completed([{**item, "status": "completed"}])]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: sse(events))) as client:
        observation = await request(client, grant(), model="test", input_items=[])
    assert observation.output == [item]
    assert observation.report()["text_characters"] == 7


async def test_output_item_after_terminal_is_not_accepted():
    events = [completed([]), item_done(0, message("OURO_OK"))]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: sse(events))) as client:
        with pytest.raises(ProbeError, match="output_after_terminal"):
            await request(client, grant(), model="test", input_items=[])


@pytest.mark.parametrize(
    "events,code",
    [
        ([item_done(0, message("OURO_OK"))], "missing_terminal"),
        (
            [item_done(0, message("OURO_OK")), {"type": "response.incomplete"}],
            "response_not_completed",
        ),
        (
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": message("OURO_OK"),
                },
                item_done(0, {**message("OURO_OK"), "id": "other-secret"}),
                completed([]),
            ],
            "conflicting_output_item",
        ),
        (
            [item_done(0, message("OURO_OK")), item_done(1, message("OURO_OK")), completed([])],
            "conflicting_output_item",
        ),
        ([item_done(2, message("OURO_OK")), completed([])], "unfinished_output_item"),
        (
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "function_call", "id": "tool-secret", "arguments": "{"},
                },
                completed([]),
            ],
            "unfinished_output_item",
        ),
        (
            [item_done(0, {**message("OURO_OK"), "status": "incomplete"}), completed([])],
            "unfinished_output_item",
        ),
        ([item_done(True, message("OURO_OK")), completed([])], "invalid_output_item"),
        ([item_done(-1, message("OURO_OK")), completed([])], "invalid_output_item"),
        ([item_done(0, None), completed([])], "invalid_output_item"),
    ],
)
async def test_item_collection_rejects_conflicts_and_unfinished_outputs(events, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: sse(events))) as client:
        with pytest.raises(ProbeError, match=code) as failure:
            await request(client, grant(), model="test", input_items=[])
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize("text", ["", "private-unexpected-response", "NOT_OURO_OK"])
async def test_failed_text_check_preserves_safe_observation(text):
    observations = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: sse([completed([message(text)])]))
    ) as client:
        with pytest.raises(ProbeError, match="unexpected_text_result"):
            await verify_responses(client, grant(), model="test", observations=observations)
    assert len(observations) == 1
    assert observations[0]["scenario"] == "minimal_sse"
    assert observations[0]["validated"] is False
    assert observations[0]["text_characters"] == len(text)
    assert observations[0]["content_types"] == ["output_text"]
    assert observations[0]["item_types"] == ["message"]
    assert "private" not in json.dumps(observations)
    assert "message-secret" not in json.dumps(observations)


@pytest.mark.parametrize(
    "events,code",
    [
        ([{"type": "response.output_text.delta", "delta": "private"}], "missing_terminal"),
        ([{"type": "response.failed", "error": "private"}], "response_not_completed"),
        ([{"type": "error", "message": "private"}], "response_not_completed"),
        ([{"type": "response.incomplete", "output": []}], "response_not_completed"),
        ([completed([]), completed([])], "duplicate_terminal"),
        (
            [{"type": "response.completed", "response": {"status": "in_progress"}}],
            "invalid_terminal",
        ),
        (
            [{"type": "response.completed", "response": {"status": "completed", "output": []}}],
            "missing_usage",
        ),
        (
            [
                {
                    "type": "response.completed",
                    "response": {"status": "completed", "output": "invalid"},
                }
            ],
            "invalid_output",
        ),
    ],
)
async def test_stream_failure_is_never_reported_as_completion(events, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: sse(events))) as client:
        with pytest.raises(ProbeError, match=code):
            await request(client, grant(), model="test", input_items=[])


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_model_http_errors_are_safe_and_never_redirect_or_retry(status):
    calls = []

    def handler(incoming):
        calls.append(incoming)
        return httpx.Response(
            status, text="account-secret access-secret", headers={"location": "https://evil.test"}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        with pytest.raises(ProbeError) as failure:
            await request(client, grant(), model="test", input_items=[])
    assert failure.value.status == status and str(failure.value) == "model_http_error"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "call",
    [
        {"name": "arbitrary_shell", "arguments": '{"round":1}', "call_id": "a"},
        {"name": "ouro_probe_echo", "arguments": '{"round":true}', "call_id": "a"},
        {"name": "ouro_probe_echo", "arguments": '{"round":1,"extra":"secret"}', "call_id": "a"},
        {"name": "ouro_probe_echo", "arguments": '{"round":', "call_id": "a"},
        {"name": "ouro_probe_echo", "arguments": '{"round":1}', "id": "item-is-not-call-id"},
    ],
)
async def test_invalid_tool_call_never_produces_a_tool_result(call):
    calls = []

    def handler(incoming):
        calls.append(incoming)
        output = [message("OURO_OK")] if len(calls) == 1 else [{"type": "function_call", **call}]
        return sse([completed(output)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProbeError):
            await verify_responses(client, grant(), model="test")
    assert len(calls) == 2


async def test_cancelling_stream_closes_connection_without_replay():
    started, closed = asyncio.Event(), asyncio.Event()

    class PendingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            yield b'data: {"type":"response.created"}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=PendingStream()))
    ) as client:
        task = asyncio.create_task(request(client, grant(), model="test", input_items=[]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
