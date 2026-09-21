from __future__ import annotations

import asyncio
import json
from contextlib import aclosing

import httpx
import pytest

from ouro.auth.contracts import CredentialGrant
from ouro.auth.request_auth import StaticRequestAuthenticator
from ouro.config import ProviderConfigManager, ProviderRuntimeOptions
from ouro.config.reasoning import (
    mapped_reasoning,
    openai_chat_completions_effort_reasoning,
    reasoning_preset,
)
from ouro.core.context import ContextView
from ouro.core.errors import ConfigError
from ouro.core.messages import ModelMessage
from ouro.core.provider import ModelMetadata, ProviderRetryPolicy
from ouro.core.reasoning import ReasoningRequest
from ouro.providers.factory import build_runtime_adapter
from ouro.providers.http_transport import HttpxProviderHTTPTransport, ProviderTransportError
from ouro.providers.openai_responses import OpenAIResponsesProvider
from tests.auth.test_request_auth import authenticated
from tests.providers.responses_fixtures import delta, message, reasoning, terminal

MODEL = ModelMetadata(model_id="model", provider_id="fixture")
PARTIAL = delta("response.output_text.delta", 0, "msg", "partial", content_index=0)


class StreamTransport:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.requests = []

    async def stream_json(self, **kwargs):
        self.requests.append(kwargs)
        try:
            for chunk in self.chunks:
                yield chunk
        finally:
            self.closed = True


async def collect(provider, messages=None, model=MODEL):
    return [event async for event in provider.stream(model=model, messages=messages or [])]


@pytest.mark.parametrize("streaming", [False, True])
async def test_service_rejecting_explicit_truncation_receives_protocol_default_and_output_limit(
    streaming,
):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "truncation" in payload:
            return httpx.Response(400, json={"detail": "Unsupported parameter: truncation"})
        response = terminal([message()])
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(response)}\n\n",
            )
        return httpx.Response(200, json=response["response"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIResponsesProvider(
            base_url="https://fixture.invalid",
            transport=HttpxProviderHTTPTransport(client),
        )
        model = MODEL.model_copy(
            update={"supports_streaming": streaming, "max_output_tokens": 8192}
        )
        events = await collect(provider, model=model)
        assert events[-1].type == "done"
        assert len(requests) == 1
        assert requests[0]["max_output_tokens"] == 8192
        assert "truncation" not in requests[0]


async def test_closing_outer_stream_immediately_releases_transport():
    transport = StreamTransport([PARTIAL, terminal([message("partial")])])
    provider = OpenAIResponsesProvider(base_url="https://fixture.invalid", transport=transport)
    async with aclosing(provider.stream(model=MODEL, messages=[])) as events:
        assert (await anext(events)).text == "partial"
        assert not transport.closed
    assert transport.closed


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("private_headers", [False, True])
async def test_stalled_stream_times_out_or_cancels_and_closes_without_replay(
    cancel, private_headers
):
    started = asyncio.Event()

    class StalledTransport(StreamTransport):
        async def stream_json(self, **kwargs):
            self.requests.append(kwargs)
            try:
                yield PARTIAL
                started.set()
                await asyncio.Event().wait()
            finally:
                self.closed = True

    transport = StalledTransport([])
    provider = OpenAIResponsesProvider(
        base_url="https://fixture.invalid",
        transport=transport,
        timeout=10 if cancel else 0.03,
        headers=(
            {"x-conversation-id": {"source": "literal", "value": "private-session"}}
            if private_headers
            else None
        ),
    )
    task = asyncio.create_task(collect(provider))
    await asyncio.wait_for(started.wait(), 1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        events = await asyncio.wait_for(task, 1)
        assert [event.type for event in events] == ["text_delta", "error"]
        assert "timed out" in events[-1].error
        assert events[-1].error_code == "provider_response_timeout"
        if private_headers:
            assert events[-1].error == (
                "Model response timed out after 0.03 seconds (total request timeout)."
            )
        assert "private-session" not in repr(events)
    assert transport.closed and len(transport.requests) == 1


async def test_timeout_does_not_cancel_consumer_while_it_processes_an_event():
    transport = StreamTransport([PARTIAL, terminal([message("partial")])])
    provider = OpenAIResponsesProvider(
        base_url="https://fixture.invalid",
        transport=transport,
        timeout=0.01,
    )
    async with aclosing(provider.stream(model=MODEL, messages=[])) as events:
        await anext(events)
        await asyncio.sleep(0.03)
    assert transport.closed


async def test_timeout_before_output_uses_shared_bounded_retry_budget():
    class StalledTransport(StreamTransport):
        async def stream_json(self, **kwargs):
            self.requests.append(kwargs)
            await asyncio.Event().wait()
            yield {}  # Makes this a streaming transport.

    transport = StalledTransport([])
    provider = OpenAIResponsesProvider(
        base_url="https://fixture.invalid",
        transport=transport,
        timeout=0.01,
        retry_policy=ProviderRetryPolicy(max_attempts=2, initial_backoff_seconds=0),
    )
    events = await asyncio.wait_for(collect(provider), 1)
    assert [event.type for event in events] == ["retry_scheduled", "error"]
    assert len(transport.requests) == 2


async def test_static_authorization_lifetime_binds_opaque_state_without_key_derivation():
    auth = StaticRequestAuthenticator({"x-api-key": "private-test-key"})
    transport = StreamTransport([terminal([reasoning(), message()])])
    provider = OpenAIResponsesProvider(
        base_url="https://fixture.invalid",
        authenticator=auth,
        transport=transport,
    )
    events = await collect(provider)
    artifact = next(event.continuation for event in events if event.type == "continuation")
    history = [ModelMessage(role="assistant", provider_continuation=artifact)]
    assert "private-test-key" not in artifact.model_dump_json()
    assert (await collect(provider, history))[-1].type == "done"
    assert transport.requests[-1]["payload"]["input"] == [reasoning(), message()]
    provider.authenticator = StaticRequestAuthenticator({"x-api-key": "private-test-key"})
    events = await collect(provider, history)
    assert events[-1].error_code == "continuation_scope_mismatch"
    assert len(transport.requests) == 2


async def test_managed_refresh_keeps_scope_and_account_change_rejects_before_http(tmp_path):
    service, record, auth = await authenticated(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 2:
            return httpx.Response(401)
        return httpx.Response(200, json=terminal([reasoning(), message()])["response"])

    model = MODEL.model_copy(update={"supports_streaming": False})
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIResponsesProvider(
                base_url="https://fixture.invalid",
                api_path="/api/responses",
                provider_id="fixture",
                authenticator=auth,
                transport=HttpxProviderHTTPTransport(client),
            )
            events = await collect(provider, model=model)
            artifact = next(event.continuation for event in events if event.type == "continuation")
            history = [ModelMessage(role="assistant", provider_continuation=artifact)]
            events = await collect(provider, history, model)
            renewed = next(event.continuation for event in events if event.type == "continuation")
            assert renewed.blocks[0]["scope"] == artifact.blocks[0]["scope"]
            assert requests[1].headers["authorization"] == "Bearer fixture-access"
            assert requests[2].headers["authorization"] == "Bearer fixture-rotated"
            assert json.loads(requests[1].content) == json.loads(requests[2].content)
            await service.commit(
                record.credential_id,
                record.driver_id,
                CredentialGrant(
                    secret_payload={
                        "access": "other-access",
                        "refresh": "other-refresh",
                        "subject": "other-account",
                    },
                ),
                expected_revision=2,
            )
            events = await collect(provider, history, model)
            assert events[-1].error_code == "continuation_scope_mismatch"
            assert "start a new session" in events[-1].error
            assert len(requests) == 3
    finally:
        await service.close()


async def test_in_flight_account_replacement_does_not_relabel_response_scope(tmp_path):
    service, record, auth = await authenticated(tmp_path)
    initial = await auth.acquire("https://fixture.invalid/api/responses")

    async def handler(request):
        assert request.headers["authorization"] == "Bearer fixture-access"
        await service.commit(
            record.credential_id,
            record.driver_id,
            CredentialGrant(
                secret_payload={
                    "access": "replacement",
                    "refresh": "replacement",
                    "subject": "account-b",
                }
            ),
            expected_revision=1,
        )
        return httpx.Response(200, json=terminal([message()])["response"])

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAIResponsesProvider(
                base_url="https://fixture.invalid",
                api_path="/api/responses",
                authenticator=auth,
                transport=HttpxProviderHTTPTransport(client),
            )
            events = await collect(
                provider, model=MODEL.model_copy(update={"supports_streaming": False})
            )
            artifact = next(event.continuation for event in events if event.type == "continuation")
            assert tuple(artifact.blocks[0]["scope"]) == initial.scope
            current = await auth.acquire("https://fixture.invalid/api/responses")
            assert current.scope != initial.scope
    finally:
        await service.close()


@pytest.mark.parametrize(
    "options",
    [
        {"store": True},
        {"input": []},
        {"previous_response_id": "remote"},
        {"max_output_tokens": 10},
        {"background": True},
        {"truncation": "auto"},
    ],
)
async def test_owned_controls_rejected_before_acquiring_credentials(options):
    class UnusedAuth:
        async def acquire(self, url):
            pytest.fail("invalid config must not acquire credentials")

    provider = OpenAIResponsesProvider(
        base_url="https://fixture.invalid",
        authenticator=UnusedAuth(),
        request_options=options,
        transport=StreamTransport([]),
    )
    events = await collect(provider)
    assert events[-1].type == "error"
    assert "protocol-owned" in events[-1].error


async def test_compatible_config_factory_encodes_native_reasoning_without_product_coupling(
    tmp_path,
):
    contract = mapped_reasoning(
        name="fixture-native",
        activation="always_on",
        output="summary",
        continuation="opaque",
        supported_efforts=("low",),
        default_effort="low",
        variants={
            "enabled": {"reasoning": {"effort": "low", "summary": "auto"}},
            "low": {"reasoning": {"effort": "low", "summary": "auto"}},
        },
    )
    manager = ProviderConfigManager(tmp_path)
    manager.create_provider(
        "fixture",
        provider_type="custom",
        protocol="openai-responses",
        base_url="https://fixture.invalid/v1",
    )
    manager.upsert_endpoint(
        "fixture",
        "main",
        model_id="model",
        reasoning=contract,
        max_output_tokens=256,
        temperature=0,
        make_default=True,
    )
    options = manager.resolve_runtime_options(ProviderRuntimeOptions(provider_id="fixture"))
    adapter = build_runtime_adapter(options)
    transport = StreamTransport([terminal([reasoning(), message()])])
    adapter.provider.transport = transport
    try:
        events = [
            event
            async for event in adapter.stream(
                ContextView(
                    system_prompt="instructions",
                    model_messages=[ModelMessage.text(role="user", text="hello")],
                )
            )
        ]
        assert [event.type for event in events] == [
            "reasoning_delta",
            "text_delta",
            "continuation",
            "done",
        ]
        payload = transport.requests[0]["payload"]
        assert transport.requests[0]["url"] == "https://fixture.invalid/v1/responses"
        assert payload["reasoning"] == {"effort": "low", "summary": "auto"}
        assert payload["max_output_tokens"] == 256 and payload["temperature"] == 0
        assert payload["store"] is False and "truncation" not in payload
        assert payload["include"] == ["reasoning.encrypted_content"]
        assert payload["instructions"] == "instructions"
        assert payload["input"] == [{"role": "user", "content": "hello"}]
        assert events[-1].usage.estimated_cost is None
    finally:
        await adapter.provider.close()


def test_factory_rejects_chat_reasoning_profile_for_responses():
    with pytest.raises(ConfigError, match="reasoning object"):
        build_runtime_adapter(
            ProviderRuntimeOptions(
                protocol="openai-responses",
                model="model",
                base_url="https://fixture.invalid",
                reasoning=openai_chat_completions_effort_reasoning(),
                reasoning_request=ReasoningRequest(mode="enabled"),
            )
        )


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    ("intent", "native_effort"),
    [
        (ReasoningRequest(), None),
        (ReasoningRequest(mode="enabled"), "medium"),
        (ReasoningRequest(mode="disabled"), "none"),
        *[
            (ReasoningRequest(effort=effort), effort)
            for effort in ("minimal", "low", "medium", "high", "xhigh", "max")
        ],
    ],
)
async def test_responses_preset_roundtrips_config_and_encodes_requested_effort(
    tmp_path, streaming, intent, native_effort
):
    manager = ProviderConfigManager(tmp_path)
    manager.create_provider(
        "fixture",
        provider_type="custom",
        protocol="openai-responses",
        base_url="https://fixture.invalid/v1",
    )
    manager.upsert_endpoint(
        "fixture",
        "main",
        model_id="custom-model",
        reasoning=reasoning_preset("openai-responses-effort"),
        supports_streaming=streaming,
        make_default=True,
    )
    options = ProviderConfigManager(tmp_path).resolve_runtime_options(
        ProviderRuntimeOptions(provider_id="fixture", reasoning_request=intent)
    )
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        response = terminal([message()])
        if streaming:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(response)}\n\n",
            )
        return httpx.Response(200, json=response["response"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = build_runtime_adapter(options)
        await adapter.provider.close()
        adapter.provider.transport = HttpxProviderHTTPTransport(client)
        events = [
            event
            async for event in adapter.stream(
                ContextView(model_messages=[ModelMessage.text(role="user", text="hello")])
            )
        ]
    assert events[-1].type == "done"
    assert len(requests) == 1
    payload = requests[0]
    if native_effort is None:
        assert "reasoning" not in payload
    else:
        assert payload["reasoning"] == {"effort": native_effort}
    assert "reasoning_effort" not in payload
    assert payload["include"] == ["reasoning.encrypted_content"]


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("preset", ["openai-responses-effort", "openai-responses-effort-summary"])
async def test_responses_visible_reasoning_reaches_adapter_without_becoming_answer(
    tmp_path, monkeypatch, streaming, preset
):
    manager = ProviderConfigManager(tmp_path)
    manager.create_provider(
        "fixture",
        provider_type="custom",
        protocol="openai-responses",
        base_url="https://fixture.invalid/v1",
    )
    manager.upsert_endpoint(
        "fixture",
        "main",
        model_id="model",
        reasoning=reasoning_preset(preset),
        supports_streaming=streaming,
        make_default=True,
    )
    requests = []
    output = [
        reasoning(summary=[], content=[{"type": "reasoning_text", "text": "analysis"}]),
        message(),
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        if streaming:
            chunks = [
                delta("response.reasoning_text.delta", 0, "rs", "ana", content_index=0),
                terminal(output),
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks),
            )
        return httpx.Response(200, json=terminal(output)["response"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            "ouro.providers.openai_responses.HttpxProviderHTTPTransport",
            lambda: HttpxProviderHTTPTransport(client),
        )
        adapter = build_runtime_adapter(
            ProviderConfigManager(tmp_path).resolve_runtime_options(
                ProviderRuntimeOptions(provider_id="fixture")
            )
        )
        try:
            events = [
                event
                async for event in adapter.stream(
                    ContextView(model_messages=[ModelMessage.text(role="user", text="hello")])
                )
            ]
        finally:
            await adapter.provider.close()
    assert events[-1].type == "done"
    assert "".join(event.reasoning_content or "" for event in events) == "analysis"
    assert "".join(event.text or "" for event in events) == "hello"
    continuation = next(event.continuation for event in events if event.type == "continuation")
    assert continuation.blocks[0]["items"] == output
    if preset == "openai-responses-effort":
        assert "reasoning" not in requests[0]
    else:
        assert requests[0]["reasoning"] == {"summary": "auto"}


@pytest.mark.parametrize(
    "options", [{"text": "invalid"}, {"text": {"format": {"type": "json_schema"}}}]
)
def test_factory_validates_structured_output_contract(options):
    with pytest.raises(ConfigError, match="text"):
        build_runtime_adapter(
            ProviderRuntimeOptions(
                protocol="openai-responses",
                model="model",
                base_url="https://fixture.invalid",
                extra=options,
            )
        )


@pytest.mark.parametrize(
    "lane,coordinate",
    [("reasoning_summary_text", "summary_index"), ("reasoning_text", "content_index")],
)
async def test_network_failure_after_hidden_reasoning_does_not_replay_response(lane, coordinate):
    class DroppingTransport(StreamTransport):
        async def stream_json(self, **kwargs):
            self.requests.append(kwargs)
            yield delta(f"response.{lane}.delta", 0, "rs", "checking", **{coordinate: 0})
            raise ProviderTransportError("disconnected", retryable=True)

    transport = DroppingTransport([])
    provider = OpenAIResponsesProvider(
        base_url="https://fixture.invalid",
        transport=transport,
        retry_policy=ProviderRetryPolicy(max_attempts=2, initial_backoff_seconds=0),
    )
    events = await collect(provider)
    assert [event.type for event in events] == ["error"]
    assert len(transport.requests) == 1
