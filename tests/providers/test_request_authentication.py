from __future__ import annotations

import json

import httpx
import pytest

from ouro.core.provider import ModelMetadata, ProviderRetryPolicy
from ouro.providers.anthropic_messages import AnthropicMessagesProvider
from ouro.providers.authentication import ProviderRequestPolicy
from ouro.providers.http_transport import HttpxProviderHTTPTransport, ProviderTransportError
from ouro.providers.openai_chat_completions import OpenAIChatCompletionsProvider
from ouro.providers.openai_responses import OpenAIResponsesProvider
from tests.auth.test_driver_contracts import BrowserFixtureDriver, DeviceFixtureDriver
from tests.auth.test_request_auth import authenticated
from tests.providers.responses_fixtures import delta, message, terminal

PROVIDERS = [OpenAIChatCompletionsProvider, AnthropicMessagesProvider, OpenAIResponsesProvider]


def response(provider, streaming):
    if provider is OpenAIResponsesProvider:
        completed = terminal([message("ok")])
        body = completed["response"]
        chunks = [completed]
    elif provider is OpenAIChatCompletionsProvider:
        body = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        chunks = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}]
    else:
        body = {
            "type": "message",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
        }
        chunks = [
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
    if not streaming:
        return httpx.Response(200, json=body)
    return httpx.Response(200, text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks))


def build(provider, auth, client, **kwargs):
    return provider(
        base_url="https://fixture.invalid",
        api_path="/api/messages",
        authenticator=auth,
        transport=HttpxProviderHTTPTransport(client),
        retry_policy=ProviderRetryPolicy(initial_backoff_seconds=0, max_backoff_seconds=0),
        **kwargs,
    )


async def collect(adapter, streaming=False):
    return [
        event
        async for event in adapter.stream(
            model=ModelMetadata(
                model_id="fixture", provider_id="fixture", supports_streaming=streaming
            ),
            messages=[],
        )
    ]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("driver", [BrowserFixtureDriver(), DeviceFixtureDriver()])
async def test_each_protocol_uses_each_driver_and_rechecks_logout(
    tmp_path, provider, streaming, driver
):
    service, record, auth = await authenticated(tmp_path, driver)
    requests = []

    def handler(request):
        requests.append(request)
        return response(provider, streaming)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = build(provider, auth, client)
            events = await collect(adapter, streaming)
            expected = (
                ["text_delta", "continuation", "done"]
                if provider is OpenAIResponsesProvider
                else ["text_delta", "done"]
            )
            assert [event.type for event in events] == expected
            for name, value in driver.encode(record).headers.items():
                assert requests[0].headers[name] == value
            await adapter.close()
            assert not client.is_closed
            assert await service.status(record.credential_id)  # Borrowed auth is still open.
            await service.logout(record.credential_id, record.driver_id)
            events = await collect(adapter, streaming)
            assert events[-1].error_code == "authentication_required"
            assert len(requests) == 1
    finally:
        await service.close()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "statuses,limit,expected,attempts",
    [
        ([401, 200], 2, None, 2),
        ([401, 401, 200], 5, "authentication_required", 2),
        ([500, 401, 200], 2, "authentication_required", 2),
        ([500, 401, 200], 3, None, 3),
        ([401, 200], 1, "authentication_required", 1),
        ([403, 200], 3, "access_denied", 1),
    ],
)
async def test_auth_recovery_shares_model_budget_and_never_repeats(
    tmp_path, provider, streaming, statuses, limit, expected, attempts
):
    service, record, auth = await authenticated(tmp_path)
    requests = []

    def handler(request):
        status = statuses[len(requests)]
        requests.append(request)
        if status == 200:
            return response(provider, streaming)
        return httpx.Response(status, json={"error": {"message": "fixture-access raw secret"}})

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = build(provider, auth, client)
            adapter.retry_policy.max_attempts = limit
            events = await collect(adapter, streaming)
            assert len(requests) == attempts
            assert events[-1].error_code == expected
            assert events[-1].type == ("error" if expected else "done")
            assert "fixture-access" not in repr(events)
            current = await service.read(record.credential_id)
            assert current.revision <= 2
            if len(requests) > 1 and statuses[0] == 401:
                assert requests[1].headers["authorization"] == "Bearer fixture-rotated"
    finally:
        await service.close()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "code,delay,expected,attempts",
    [
        ("fixture_quota", "0", "quota_exhausted", 1),
        ("fixture_rate", "600", "rate_limited", 1),
        ("fixture_rate", "0", None, 2),
    ],
)
async def test_service_quota_classification_and_bounded_retry_after(
    tmp_path, provider, code, delay, expected, attempts
):
    service, _, auth = await authenticated(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": delay},
                json={"error": {"code": code, "message": "fixture-access"}},
            )
        return response(provider, False)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = build(
                provider,
                auth,
                client,
                request_policy=ProviderRequestPolicy(
                    quota_error_codes=frozenset({"fixture_quota"}),
                ),
            )
            events = await collect(adapter)
            assert events[-1].error_code == expected
            assert len(requests) == attempts
            assert "fixture-access" not in repr(events)
    finally:
        await service.close()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "headers",
    [
        {"AUTHORIZATION": "override"},
        {"host": "attacker.invalid"},
        {"cookie": "identity=other"},
    ],
)
async def test_header_overrides_fail_before_http(tmp_path, provider, headers):
    service, _, auth = await authenticated(tmp_path)

    def handler(request):
        pytest.fail("credential/routing override must fail before HTTP")

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = await collect(build(provider, auth, client, default_headers=headers))
            assert events[-1].type == "error"
            assert "override" in events[-1].error
    finally:
        await service.close()


@pytest.mark.parametrize("streaming", [False, True])
async def test_transport_disables_redirects_even_on_an_injected_redirecting_client(streaming):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://attacker.invalid/collect"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        transport = HttpxProviderHTTPTransport(client)
        kwargs = dict(
            url="https://fixture.invalid/api/messages",
            headers={"authorization": "secret"},
            payload={},
            timeout=1,
        )
        with pytest.raises(ProviderTransportError):
            if streaming:
                _ = [chunk async for chunk in transport.stream_json(**kwargs)]
            else:
                await transport.post_json(**kwargs)
        assert len(requests) == 1


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_partial_output_is_never_replayed_or_refreshed(tmp_path, provider):
    service, record, auth = await authenticated(tmp_path)

    class DroppingTransport:
        calls = 0

        async def stream_json(self, **kwargs):
            self.calls += 1
            if provider is OpenAIResponsesProvider:
                yield delta("response.output_text.delta", 0, "msg", "partial", content_index=0)
            elif provider is OpenAIChatCompletionsProvider:
                yield {"choices": [{"delta": {"content": "partial"}}]}
            else:
                yield {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "partial"},
                }
            raise ProviderTransportError("fixture-access leaked", retryable=True, status_code=401)

    transport = DroppingTransport()
    try:
        adapter = provider(
            base_url="https://fixture.invalid",
            api_path="/api/messages",
            authenticator=auth,
            transport=transport,
        )
        events = await collect(adapter, True)
        assert [event.type for event in events] == ["text_delta", "error"]
        assert events[-1].error_code == "authentication_required"
        assert transport.calls == 1
        assert (await service.read(record.credential_id)).revision == 1
        assert "fixture-access" not in repr(events)
    finally:
        await service.close()


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_check_uses_same_recovery_and_safe_error_boundary(tmp_path, provider):
    service, _, auth = await authenticated(tmp_path)
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        if provider is OpenAIResponsesProvider:
            assert payload["max_output_tokens"] == 8192
        else:
            assert payload["max_tokens"] == 1
        return httpx.Response(401, text="fixture-access")

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await build(provider, auth, client).check_connection(
                ModelMetadata(model_id="fixture", provider_id="fixture")
            )
            assert not result.ok and result.error_code == "authentication_required"
            assert len(requests) == 2 and "fixture-access" not in result.message
    finally:
        await service.close()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_in_band_error_body_cannot_leak_secrets(tmp_path, provider, streaming):
    service, _, auth = await authenticated(tmp_path)
    body = {"type": "error", "error": {"message": "fixture-access", "type": "unknown_error"}}

    def handler(request):
        if streaming:
            return httpx.Response(200, text=f"data: {json.dumps(body)}\n\n")
        return httpx.Response(200, json=body)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            events = await collect(build(provider, auth, client), streaming)
            assert events[-1].type == "error"
            expected = (
                ("response_failed" if streaming else "invalid_terminal")
                if provider is OpenAIResponsesProvider
                else "provider_response_failed"
            )
            assert events[-1].error_code == expected
            assert "fixture-access" not in repr(events)
    finally:
        await service.close()


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_managed_network_failure_remains_classified_after_budget_exhaustion(
    tmp_path, provider
):
    service, _, auth = await authenticated(tmp_path)

    def handler(request):
        raise httpx.ConnectError("fixture-access", request=request)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = build(provider, auth, client)
            adapter.retry_policy.max_attempts = 1
            events = await collect(adapter)
            assert events[-1].error_code == "provider_connection_failed"
            assert events[-1].error == "Model connection could not be established (ConnectError)."
            assert "fixture-access" not in repr(events)
    finally:
        await service.close()


async def test_retry_after_is_honored_within_the_existing_backoff_bound(tmp_path, monkeypatch):
    service, _, auth = await authenticated(tmp_path)
    delays = []
    requests = []

    async def sleep(delay):
        delays.append(delay)

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return response(OpenAIChatCompletionsProvider, False)

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = build(OpenAIChatCompletionsProvider, auth, client)
            adapter.retry_policy.max_backoff_seconds = 5
            monkeypatch.setattr("ouro.providers.retrying.asyncio.sleep", sleep)
            events = await collect(adapter)
            assert events[-1].type == "done"
            assert delays == [3] and events[0].retry.delay_seconds == 3
    finally:
        await service.close()
