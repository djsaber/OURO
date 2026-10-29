from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime

import httpx
import pytest

from ouro.application.sessions.service import SessionService
from ouro.auth.managed_requests import RequestTarget
from ouro.config import ProviderConfigManager, provider_models
from ouro.config.compaction import CompactionConfigManager
from ouro.config.provider_models import ProviderConnectionSpec, ProviderTypeSpec
from ouro.config.reasoning import mapped_reasoning
from ouro.core.billing import UsageFilter
from ouro.core.provider import ProviderRetryPolicy
from ouro.core.reasoning import ReasoningRequest
from ouro.local_application import LocalApplication, ProviderServicePolicy, build_local_runtime
from ouro.providers.authentication import ProviderRequestPolicy
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from ouro.tools import Tool, ToolDefinition, ToolResult
from tests.application.test_runtime_authentication import login
from tests.auth import test_driver_contracts as auth_fixtures
from tests.event_capture import recorded_events
from tests.providers.responses_fixtures import call, delta, item_event, message, reasoning, terminal
from tests.run_facts import run_events


@pytest.fixture(params=[("files", False), ("files", True), ("sqlite", False), ("sqlite", True)])
async def responses_runtime(tmp_path, monkeypatch, request):
    backend, streaming = request.param
    monkeypatch.setattr(auth_fixtures, "NOW", datetime.now(UTC))
    driver = auth_fixtures.BrowserFixtureDriver()
    monkeypatch.setitem(
        provider_models._PROVIDER_TYPE_SPECS,
        "custom",
        ProviderTypeSpec(
            "custom",
            ProviderConnectionSpec("openai-chat-completions"),
            (ProviderConnectionSpec("openai-responses", "fixture-browser", "fixture"),),
        ),
    )
    policies = (
        ProviderServicePolicy(
            "fixture",
            (RequestTarget("https://fixture.invalid", "/api"),),
            ProviderRequestPolicy(billing_mode="subscription", supports_output_limit=False),
        ),
    )
    configs = ProviderConfigManager(tmp_path)
    provider = configs.create_provider(
        display_name="Subscription",
        provider_type="custom",
        protocol="openai-responses",
        auth_driver="fixture-browser",
        base_url="https://fixture.invalid/api",
    )
    endpoint_ids = {}
    for endpoint in ("chat", "summary"):
        configured = configs.create_endpoint(
            provider.provider_id,
            model_id=endpoint,
            supports_streaming=streaming,
            supports_tools=True,
            max_output_tokens=1024,
            reasoning=mapped_reasoning(
                name="fixture-reasoning",
                activation="always_on",
                default=ReasoningRequest(mode="enabled"),
                effort={"enabled": {"reasoning": {"effort": "low"}}},
            ),
        )
        endpoint_ids[endpoint] = configured.endpoint_id
    configs.set_default_route(provider.provider_id, endpoint_ids["chat"])
    CompactionConfigManager(tmp_path).update_model(
        provider_id=provider.provider_id, endpoint_id=endpoint_ids["summary"]
    )
    replies = deque()
    requests = []
    executions = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append((payload, dict(request.headers)))
        reply = replies.popleft()
        if isinstance(reply, int):
            return httpx.Response(reply)
        if isinstance(reply, httpx.Response):
            return reply
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json=reply)
        assert "max_output_tokens" not in payload
        if not payload["stream"]:
            return httpx.Response(200, json=reply["response"])
        # Exercise the observed SSE form where the terminal has no output snapshot.
        items = reply["response"].pop("output")
        events = [item_event(index, item) for index, item in enumerate(items)] + [reply]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(event)}\n\n" for event in events),
        )

    def echo(invocation, context):
        executions.append(invocation.arguments["round"])
        return ToolResult(
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=f"result-{invocation.arguments['round']}",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            "ouro.providers.openai_responses.adapter.HttpxProviderHTTPTransport",
            lambda: HttpxProviderHTTPTransport(client),
        )
        monkeypatch.setattr(
            "ouro.providers.openai_chat_completions.adapter.HttpxProviderHTTPTransport",
            lambda: HttpxProviderHTTPTransport(client),
        )

        def build(*, timeout=None):
            if timeout is not None:
                configs.update_provider(provider.provider_id, timeout_seconds=timeout)
            runtime = build_local_runtime(
                tmp_path,
                session_backend=backend,
                auth_drivers=(driver,),
                service_policies=policies,
                system_prompt="Use the provided tools to complete the task.",
            )
            runtime.tools.register(
                Tool(
                    definition=ToolDefinition(
                        name="echo",
                        description="Return the round number without side effects.",
                        effect="read_only",
                        parameters_schema={
                            "type": "object",
                            "properties": {"round": {"type": "integer"}},
                            "required": ["round"],
                        },
                    ),
                    handler=echo,
                )
            )
            return LocalApplication.from_runtime(runtime)

        yield build, replies, requests, executions


@pytest.mark.parametrize("responses_runtime", [("files", True), ("sqlite", True)], indirect=True)
@pytest.mark.parametrize("stall", [False, True])
async def test_long_tool_arguments_execute_only_after_successful_completion(
    responses_runtime, stall
):
    build, replies, requests, executions = responses_runtime
    output = call()
    chunks = [item_event(0, {**output, "arguments": ""}, complete=False)] + [
        delta("response.function_call_arguments.delta", 0, output["id"], part)
        for part in output["arguments"]
    ]

    class ToolBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            for chunk in chunks:
                await asyncio.sleep(0.04)
                assert executions == []
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            if stall:
                await asyncio.Event().wait()
            yield f"data: {json.dumps(terminal([output]))}\n\n".encode()

        async def aclose(self):
            self.closed = True

    body = ToolBody()
    async with build(timeout=0.2) as app:
        streamed = []
        app.runtime.add_event_sink(streamed.append)
        await login(app.provider_auth)
        replies.extend([httpx.Response(200, stream=body), terminal([message("finished")])])
        result = await app.runtime.run("Call echo once.")
        await app.runtime.drain_events()
        assert body.closed
        assert (
            "".join(
                event.payload["arguments_delta"]
                for event in streamed
                if event.event_type == "model_tool_call_delta"
            )
            == output["arguments"]
        )
        if stall:
            assert result.run.status == "failed"
            assert "inactivity" in result.run.error
            assert executions == [] and len(requests) == 1
            assert app.runtime.usage.run_usage(result.session.session_id, result.run.run_id) is None
        else:
            assert result.run.status == "completed", result.run.error
            assert result.final_text == "finished"
            assert executions == [1] and len(requests) == 2
            assert (
                app.runtime.usage.run_usage(
                    result.session.session_id, result.run.run_id
                ).request_count
                == 2
            )


async def tool_run(app, replies):
    replies.extend(
        [
            terminal([reasoning(id="rs-1"), call(1)]),
            terminal([reasoning(id="rs-2"), call(2)]),
            terminal([reasoning(id="rs-3"), message("finished")]),
        ]
    )
    result = await app.runtime.run("Perform two tool rounds.")
    assert result.run.status == "completed", result.run.error
    assert result.final_text == "finished"
    return result


def output_items(payload):
    return [item for item in payload["input"] if "id" in item]


async def test_reasoning_text_is_persisted_separately_from_answer(responses_runtime):
    build, replies, _, _ = responses_runtime
    item = reasoning(summary=[], content=[{"type": "reasoning_text", "text": "analysis"}])
    async with build() as app:
        await login(app.provider_auth)
        replies.append(terminal([item, message("answer")]))
        result = await app.runtime.run("Explain briefly.")
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "answer"
        saved = app.runtime.store.load_session(result.session.session_id).messages
        assistant = next(m for m in saved if m.role == "assistant")
        assert assistant.reasoning_content == "analysis"
        assert assistant.provider_continuation.blocks[0]["items"] == [item, message("answer")]


async def test_tool_loop_persists_and_replays_after_restart_and_refresh(responses_runtime):
    build, replies, requests, executions = responses_runtime
    async with build() as app:
        await login(app.provider_auth)
        first = await tool_run(app, replies)
        session_id = first.session.session_id
        context = app.runtime.store.load_latest_context(session_id)
        assert context is not None
        assert context.metrics.reserved_output_tokens == 1024
        assert context.metrics.input_budget_tokens == context.metrics.context_window_tokens - 1024
        saved = app.runtime.store.load_session(session_id).messages
        assistants = [m for m in saved if m.role == "assistant"]
        assert len(assistants) == 3
        assert all(m.provider_continuation is not None for m in assistants)
        assert all(m.reasoning_content == "checking" for m in assistants)
        assert [m.tool_calls[0].id for m in assistants[:2]] == ["call-1", "call-2"]
        assert executions == [1, 2]
        assert output_items(requests[1][0]) == [reasoning(id="rs-1"), call(1)]
        assert output_items(requests[2][0]) == [
            reasoning(id="rs-1"),
            call(1),
            reasoning(id="rs-2"),
            call(2),
        ]
        results = [i for i in requests[2][0]["input"] if i.get("type") == "function_call_output"]
        assert [(i["call_id"], i["output"]) for i in results] == [
            ("call-1", "result-1"),
            ("call-2", "result-2"),
        ]
        assert first.usage["input_tokens"] == 60
        assert first.usage["output_tokens"] == 30
        assert (
            app.runtime.usage.run_usage(first.session.session_id, first.run.run_id).request_count
            == 3
        )
        entries = app.runtime.usage.list_entries(UsageFilter(run_ids={first.run.run_id})).entries
        assert all(entry.billing_mode == "subscription" for entry in entries)
        assert all(entry.estimated_cost is None for entry in entries)
        assert sum(entry.cache_read_tokens for entry in entries) == 15

    async with build() as app:
        assert app.runtime.store.load_session(session_id).messages == saved
        replies.extend([401, terminal([message("continued", item_id="next")])])
        continued = await app.runtime.run("Continue.", session_id=session_id)
        assert continued.run.status == "completed", continued.run.error
        assert continued.final_text == "continued"
        assert executions == [1, 2]
        assert requests[-2][0] == requests[-1][0]
        assert requests[-2][1]["authorization"] == "Bearer fixture-access"
        assert requests[-1][1]["authorization"] == "Bearer fixture-rotated"
        assert output_items(requests[-1][0]) == [
            item for m in assistants for item in m.provider_continuation.blocks[0]["items"]
        ]
        await app.provider_auth.logout(app.runtime.adapter.model.provider_id)
        count = len(requests)
        failed = await app.runtime.run("After logout.", session_id=session_id)
        assert failed.run.status == "failed"
        assert len(requests) == count
        assert any(
            e.payload.get("error_code") == "authentication_required"
            for e in run_events(app.runtime.store, failed)
        )
        persisted = app.runtime.store.load_session(session_id).model_dump_json()
        assert "fixture-access" not in persisted and "fixture-rotated" not in persisted
    assert not replies


async def test_incomplete_tool_response_cannot_execute_and_recovery_uses_completed_history(
    responses_runtime,
):
    build, replies, requests, executions = responses_runtime
    async with build() as app:
        await login(app.provider_auth)
        incomplete = terminal([reasoning(), call(1)], status="incomplete")
        incomplete["type"] = "response.incomplete"
        replies.append(incomplete)
        failed = await app.runtime.run("Use the tool.")
        assert failed.run.status == "failed"
        assert any(
            e.payload.get("error_code") == "response_incomplete"
            for e in run_events(app.runtime.store, failed)
        )
        assert executions == []
        assert app.runtime.usage.run_usage(failed.session.session_id, failed.run.run_id) is None
        assert len(requests) == 1
        session_id = failed.session.session_id
        saved = app.runtime.store.load_session(session_id).messages
        assert all(m.provider_continuation is None for m in saved)

    async with build() as app:
        replies.extend([terminal([call(1)]), terminal([message("recovered")])])
        result = await app.runtime.continue_run(session_id=session_id)
        assert result.run.status == "completed", result.run.error
        assert result.run.run_id == failed.run.run_id
        assert result.final_text == "recovered"
        assert executions == [1]
        assert not output_items(requests[1][0])
        assert all(
            i.get("type") not in {"function_call", "function_call_output"}
            for i in requests[1][0]["input"]
        )
        assert (
            app.runtime.usage.run_usage(result.session.session_id, result.run.run_id).request_count
            == 2
        )
    assert not replies


@pytest.mark.parametrize("source_protocol", ["openai-chat-completions", "openai-responses"])
@pytest.mark.parametrize("continue_without_input", [False, True])
async def test_model_switch_after_failure_preserves_completed_tool_frames(
    responses_runtime, tmp_path, source_protocol, continue_without_input
):
    build, replies, requests, executions = responses_runtime
    source_route = None
    if source_protocol == "openai-chat-completions":
        configs = ProviderConfigManager(tmp_path)
        source = configs.create_provider(
            display_name="Source",
            provider_type="custom",
            protocol=source_protocol,
            base_url="https://source.invalid/v1",
            retry_policy=ProviderRetryPolicy(max_attempts=1),
        )
        endpoint = configs.create_endpoint(
            source.provider_id,
            model_id="source-model",
            supports_streaming=False,
            reasoning=mapped_reasoning(
                name="source-reasoning",
                activation="always_on",
                default=ReasoningRequest(mode="enabled"),
                effort={"enabled": {}},
            ),
        )
        source_route = f"{source.provider_id}/{endpoint.endpoint_id}"
        replies.extend(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "Checking the first result.",
                                "reasoning_content": "source-private-reasoning",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {"name": "echo", "arguments": '{"round":1}'},
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                429,
            ]
        )
    else:
        failure = terminal([], status="failed")
        failure["type"] = "response.failed"
        replies.extend([terminal([reasoning(), call(1)]), failure])

    async with build() as app:
        target = next(m for m in app.runtime.models.list_models() if m.model_id == "summary")
        auth = app.provider_auth
        method = (await auth.methods(target.provider_id))[0]
        started = await auth.begin(target.provider_id, method.method_id)
        assert (await auth.wait(started.operation_id)).state == "succeeded"
        failed = await app.runtime.run("Perform two tool rounds.", initial_model=source_route)
        assert failed.run.status == "failed", failed.run.error
        assert executions == [1]
        session_id = failed.session.session_id
        saved = app.runtime.store.load_session(session_id).messages
        source_message = next(m for m in saved if m.tool_calls)
        event_cursor = len(recorded_events(session_id))
        assert source_message.provider_continuation.protocol == source_protocol
        app.runtime.switch_session_model(session_id, target.key)

    async with build() as app:
        replies.extend(
            [terminal([reasoning(id="target-rs"), call(2)]), terminal([message("done")])]
        )
        result = (
            await app.runtime.continue_run(session_id=session_id)
            if continue_without_input
            else await app.runtime.run("Finish the task.", session_id=session_id)
        )
        assert result.run.status == "completed", result.run.error
        assert result.final_text == "done"
        assert result.run.model == "summary"
        assert (result.run.run_id == failed.run.run_id) == continue_without_input
        assert executions == [1, 2]
        first_target = requests[2][0]
        assert first_target["model"] == "summary"
        assert not output_items(first_target)
        assert "source-private-reasoning" not in json.dumps(first_target)
        assert "opaque-ciphertext" not in json.dumps(first_target)
        assert [
            i["call_id"] for i in first_target["input"] if i.get("type") == "function_call"
        ] == ["call-1"]
        assert [i for i in first_target["input"] if i.get("type") == "function_call_output"] == [
            {"type": "function_call_output", "call_id": "call-1", "output": "result-1"}
        ]
        assert output_items(requests[3][0]) == [reasoning(id="target-rs"), call(2)]
        persisted = app.runtime.store.load_session(session_id).messages
        assert persisted[: len(saved)] == saved
        assert len([m for m in persisted if m.role == "user"]) == (
            1 if continue_without_input else 2
        )
        assert (
            not any(
                e.event_type == "user_message_added"
                for e in recorded_events(session_id)[event_cursor:]
            )
        ) == (continue_without_input)
    assert not replies


async def test_branch_replays_canonical_tool_history_without_source_execution_state(
    responses_runtime,
):
    build, replies, requests, executions = responses_runtime
    async with build() as app:
        await login(app.provider_auth)
        first = await tool_run(app, replies)
        source = app.runtime.store.load_session(first.session.session_id)
        tool_message = next(m for m in source.messages if m.tool_calls)
        branch = SessionService(app.runtime.store, resources=app.runtime.resources).branch(
            source.session.session_id, tool_message.message_id
        )
        replies.append(terminal([message("branched", item_id="branch-answer")]))
        result = await app.runtime.run("Continue on branch.", session_id=branch.session.session_id)
        assert result.run.status == "completed", result.run.error
        payload = requests[-1][0]
        assert not output_items(payload)
        calls = [i for i in payload["input"] if i.get("type") == "function_call"]
        results = [i for i in payload["input"] if i.get("type") == "function_call_output"]
        assert len(calls) == len(results) == 1
        assert calls[0]["call_id"] == results[0]["call_id"]
        assert results[0]["output"] == "result-1"
        assert executions == [1, 2]
        assert app.runtime.store.load_session(first.session.session_id).messages == source.messages
    assert not replies


async def test_compaction_uses_canonical_history_and_excludes_covered_opaque_items(
    responses_runtime,
):
    build, replies, requests, executions = responses_runtime
    async with build() as app:
        await login(app.provider_auth)
        first = await tool_run(app, replies)
        session_id = first.session.session_id
        saved = app.runtime.store.load_session(session_id).messages
        replies.append(
            terminal([reasoning(id="summary-rs"), message("<summary>Two rounds done.</summary>")])
        )
        compacted = await app.runtime.compact(session_id=session_id)
        assert compacted.run.status == "completed", compacted.run.error
        assert compacted.compaction is not None
        summary_payload = requests[-1][0]
        assert summary_payload["model"] == "summary"
        assert "tools" not in summary_payload
        assert not output_items(summary_payload)
        assert "opaque-ciphertext" not in json.dumps(summary_payload)
        assert "result-1" in json.dumps(summary_payload)
        assert app.runtime.store.load_session(session_id).messages == saved
        replies.append(terminal([message("after compaction", item_id="after-summary")]))
        result = await app.runtime.run("Continue after compaction.", session_id=session_id)
        assert result.run.status == "completed", result.run.error
        payload = requests[-1][0]
        assert "Two rounds done." in json.dumps(payload)
        expected = [
            item
            for m in saved[compacted.compaction.covered_message_count :]
            if m.provider_continuation is not None
            for item in m.provider_continuation.blocks[0]["items"]
        ]
        assert output_items(payload) == expected
        assert executions == [1, 2]
    assert not replies
