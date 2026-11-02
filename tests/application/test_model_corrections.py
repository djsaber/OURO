import asyncio
import json

import httpx
import pytest

from ouro.application.runtime.service import RuntimeService
from ouro.application.usage import UsageService
from ouro.billing import LocalUsageLedger
from ouro.core.adapter import RuntimeAdapter
from ouro.core.provider import ModelMetadata, ProviderStreamEvent, ToolCallDelta, Usage
from ouro.core.run_budget import RunBudgetPolicy
from ouro.providers import FauxProvider, OpenAIChatCompletionsProvider
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from ouro.sessions import LocalSessionStore, SQLiteSessionStore
from ouro.tools import Tool, ToolDefinition, ToolRegistry, ToolResult


class CorrectingProvider(FauxProvider):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures
        self.requests = []

    async def stream(self, *, messages, **kwargs):
        self.requests.append([message.model_dump(mode="json") for message in messages])
        round_number = len(self.requests)
        if round_number <= self.failures:
            yield ProviderStreamEvent(type="text_delta", text="rejected preamble")
            yield ProviderStreamEvent(type="reasoning_delta", reasoning_content="private thought")
            yield ProviderStreamEvent(
                type="tool_call_delta",
                tool_call_delta=ToolCallDelta(
                    index=0,
                    id_delta=f"bad-{round_number}",
                    name_delta="Act",
                    arguments_delta='{"value":"line\nbreak"}',
                ),
            )
            # A valid sibling must not execute when the batch is rejected.
            yield ProviderStreamEvent(
                type="tool_call_delta",
                tool_call_delta=ToolCallDelta(
                    index=1,
                    id_delta="sibling",
                    name_delta="Act",
                    arguments_delta="{}",
                ),
            )
        elif not any(message.tool_result for message in messages):
            yield ProviderStreamEvent.from_tool_call(
                {"id": "valid", "name": "Act", "arguments": {}}
            )
        else:
            yield ProviderStreamEvent(type="text_delta", text="done")
        yield ProviderStreamEvent(type="done", usage=Usage(input_tokens=10, output_tokens=2))


@pytest.fixture(params=[LocalSessionStore, SQLiteSessionStore])
def store(tmp_path, request):
    value = request.param(
        tmp_path / "session.db" if request.param is SQLiteSessionStore else tmp_path / "sessions"
    )
    value.initialize()
    return value


def runtime_for(store, provider, calls, tmp_path, *, streaming=True, **kwargs):
    tools = ToolRegistry()

    def act(invocation, context):
        calls.append(invocation.tool_call_id)
        return ToolResult(tool_call_id=invocation.tool_call_id, tool_name="Act", content="acted")

    tools.register(Tool(definition=ToolDefinition(name="Act", description="Act"), handler=act))
    return RuntimeService(
        store=store,
        adapter=RuntimeAdapter(
            provider,
            ModelMetadata(model_id="test", provider_id="test", supports_streaming=streaming),
        ),
        tools=tools,
        usage_service=UsageService(LocalUsageLedger(tmp_path / "billing")),
        **kwargs,
    )


@pytest.mark.parametrize("failures", [1, 2, 3])
async def test_correction_is_append_only_accounted_and_bounded(store, tmp_path, failures):
    provider = CorrectingProvider(failures)
    calls = []
    async with runtime_for(store, provider, calls, tmp_path) as runtime:
        result = await runtime.run("act")
        assert result.run.status == ("failed" if failures == 3 else "completed")
        assert calls == ([] if failures == 3 else ["valid"])
        assert len(provider.requests) == (3 if failures == 3 else failures + 2)
        assert result.usage["output_tokens"] == 2 * len(provider.requests)
        assert result.run.model_round_count == len(provider.requests)
        for before, after in zip(provider.requests, provider.requests[1:], strict=False):
            assert after[: len(before)] == before
        snapshot = store.load_session(result.session.session_id)
        corrections = [m for m in snapshot.messages if m.semantic_tag == "model-output-correction"]
        assert len(corrections) == failures
        assert [m.metadata["model_correction_attempt"] for m in corrections] == list(
            range(1, failures + 1)
        )
        assert all(m.role == "user" and m.scaffold for m in corrections)
        assert (
            corrections[0].metadata["rejected_response"]["tool_call_failure"]["arguments"]
            == '{"value":"line\nbreak"}'
        )
        assert all(m.text_content != "rejected preamble" for m in snapshot.messages)
        assert all(h.response_closed_at for h in snapshot.request_headers)
        assert (
            store.load_response_draft(
                result.session.session_id, snapshot.request_headers[-1].context_id
            )
            is None
        )
        for request in provider.requests:
            assert all(not m.get("provider_continuation") for m in request)
            assert all(
                c["id"] not in {"sibling", "bad-1", "bad-2", "bad-3"}
                for m in request
                for c in m["tool_calls"]
            )
        if failures == 3:
            resumed = await runtime.continue_session(session_id=result.session.session_id)
            assert resumed.run.status == "completed"
            assert calls == ["valid"]
            assert provider.requests[3][: len(provider.requests[2])] == provider.requests[2]


async def test_successful_tool_round_resets_consecutive_correction_limit(store, tmp_path):
    class AlternatingProvider(FauxProvider):
        rounds = 0

        async def stream(self, **kwargs):
            self.rounds += 1
            if self.rounds < 6:
                yield ProviderStreamEvent(
                    type="tool_call_delta",
                    tool_call_delta=ToolCallDelta(
                        index=0,
                        id_delta=f"call-{self.rounds}",
                        name_delta="Act",
                        arguments_delta="{" if self.rounds % 2 else "{}",
                    ),
                )
            else:
                yield ProviderStreamEvent(type="text_delta", text="done")
            yield ProviderStreamEvent(type="done")

    calls = []
    async with runtime_for(store, AlternatingProvider(), calls, tmp_path) as runtime:
        result = await runtime.run("act")
        assert result.run.status == "completed"
        assert calls == ["call-2", "call-4"]
        corrections = [
            m
            for m in store.load_session(result.session.session_id).messages
            if m.semantic_tag == "model-output-correction"
        ]
        assert [m.metadata["model_correction_attempt"] for m in corrections] == [1, 1, 1]


async def test_correction_survives_budget_pause_and_continue(store, tmp_path):
    provider = CorrectingProvider(2)
    calls = []
    async with runtime_for(
        store, provider, calls, tmp_path, run_budget=RunBudgetPolicy(max_model_rounds=1)
    ) as runtime:
        result = await runtime.run("act")
        assert result.run.status == "paused"
        assert len(provider.requests) == 1
        assert calls == []
    # Budgets belong to the persisted Run; changing the host default cannot lift it.
    store.save_run(
        result.run.model_copy(
            update={
                "metadata": {
                    **result.run.metadata,
                    "run_budget": RunBudgetPolicy(max_model_rounds=10).model_dump(mode="json"),
                }
            }
        )
    )
    async with runtime_for(store, provider, calls, tmp_path) as runtime:
        result = await runtime.continue_session(session_id=result.session.session_id)
        assert result.run.status == "completed"
        assert calls == ["valid"]
        assert provider.requests[1][: len(provider.requests[0])] == provider.requests[0]
        corrections = [
            m
            for m in store.load_session(result.session.session_id).messages
            if m.semantic_tag == "model-output-correction"
        ]
        assert [m.metadata["model_correction_attempt"] for m in corrections] == [1, 2]


async def test_cancel_after_correction_does_not_dispatch_retry(store, tmp_path, monkeypatch):
    provider = CorrectingProvider(1)
    calls = []
    committed = asyncio.Event()
    release = asyncio.Event()
    async with runtime_for(store, provider, calls, tmp_path) as runtime:
        original = runtime.run_lifecycle.correct_model_output

        async def correct(*args):
            result = await original(*args)
            repeated = await original(*args)
            assert repeated[:2] == result[:2]
            assert repeated[2] == ()
            committed.set()
            await release.wait()
            return result

        monkeypatch.setattr(runtime.run_lifecycle, "correct_model_output", correct)
        task = asyncio.create_task(runtime.run("act"))
        await asyncio.wait_for(committed.wait(), timeout=10)
        # The accepted abort is shared by the normal model request boundary.
        session = store.list_sessions()[0]
        signal = runtime.run_ownership.signal_for(session.active_run_id)
        abort = asyncio.create_task(runtime.abort_run(session_id=session.session_id))
        await asyncio.wait_for(signal.wait(), timeout=10)
        release.set()
        result = await asyncio.wait_for(task, timeout=10)
        await abort
        assert result.run.status == "aborted"
        assert len(provider.requests) == 1
        assert calls == []


@pytest.mark.parametrize("streaming", [True, False])
async def test_wire_retry_sends_invalid_arguments_only_as_user_text(tmp_path, streaming):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        message = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "bad",
                        "type": "function",
                        "function": {
                            "name": "Act",
                            "arguments": '{"value":"line\nbreak"}',
                        },
                    }
                ]
            }
            if len(requests) == 1
            else {"content": "corrected"}
        )
        body = {
            "choices": [
                {
                    "delta" if streaming else "message": message,
                    "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                }
            ]
        }
        return (
            httpx.Response(
                200,
                text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
            if streaming
            else httpx.Response(200, json=body)
        )

    store = LocalSessionStore(tmp_path / "sessions")
    store.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIChatCompletionsProvider(
            base_url="https://example.test", transport=HttpxProviderHTTPTransport(client=client)
        )
        async with runtime_for(store, provider, [], tmp_path, streaming=streaming) as runtime:
            result = await runtime.run("act")
            assert result.run.status == "completed"
    assert len(requests) == 2
    before, after = requests[0]["messages"], requests[1]["messages"]
    assert after[: len(before)] == before
    assert after[-1]["role"] == "user"
    assert "line\nbreak" in str(after[-1]["content"])
    assert not any(message.get("tool_calls") for message in after)
