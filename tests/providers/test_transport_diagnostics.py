from __future__ import annotations

import json
import ssl

import httpx
import pytest

from ouro.core.provider import ProviderRetryPolicy
from ouro.providers.authentication import ProviderRequestPolicy, request_failure
from ouro.providers.http_transport import HttpxProviderHTTPTransport, ProviderTransportError
from ouro.providers.openai_responses.adapter import OpenAIResponsesProvider
from tests.providers.responses_fixtures import delta
from tests.providers.test_request_authentication import PROVIDERS, collect

SECRET = "private-header-or-response-value"
HEADERS = {"x-conversation-id": {"source": "literal", "value": SECRET}}


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("private_headers", [False, True])
@pytest.mark.parametrize(
    "exception_type,code,description",
    [
        (httpx.ConnectTimeout, "provider_connect_timeout", "ConnectTimeout"),
        (httpx.ReadTimeout, "provider_read_timeout", "ReadTimeout"),
        (httpx.WriteTimeout, "provider_write_timeout", "WriteTimeout"),
        (httpx.PoolTimeout, "provider_pool_timeout", "PoolTimeout"),
        (httpx.ConnectError, "provider_connection_failed", "could not be established"),
        (httpx.ReadError, "provider_read_error", "ReadError"),
        (httpx.WriteError, "provider_write_error", "WriteError"),
        (httpx.ProxyError, "provider_proxy_error", "ProxyError"),
        (httpx.RemoteProtocolError, "provider_protocol_error", "ProtocolError"),
        (httpx.CloseError, "provider_network_error", "network request failed"),
        (ssl.SSLError, "provider_connection_failed", "ConnectError"),
    ],
)
async def test_network_failures_keep_common_safe_types_through_retries(
    provider, streaming, private_headers, exception_type, code, description
):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if private_headers:
            assert request.headers["x-conversation-id"] == SECRET
        if exception_type is ssl.SSLError:
            raise httpx.ConnectError(SECRET, request=request) from ssl.SSLError(SECRET)
        raise exception_type(SECRET, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = provider(
            base_url="https://fixture.invalid",
            headers=HEADERS if private_headers else None,
            transport=HttpxProviderHTTPTransport(client),
            retry_policy=ProviderRetryPolicy(max_attempts=2, initial_backoff_seconds=0),
        )
        events = await collect(adapter, streaming)
    assert attempts == 2
    assert [event.type for event in events] == ["retry_scheduled", "error"]
    assert events[-1].error_code == code
    assert description in events[-1].error
    assert description in events[0].retry.reason
    assert SECRET not in repr(events)


async def test_read_failure_after_partial_output_is_classified_without_replay():
    attempts = 0

    class DroppingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            event = delta("response.output_text.delta", 0, "msg", "partial", content_index=0)
            yield f"data: {json.dumps(event)}\n\n".encode()
            raise httpx.ReadError(SECRET)

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, stream=DroppingBody())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAIResponsesProvider(
            base_url="https://fixture.invalid",
            headers=HEADERS,
            transport=HttpxProviderHTTPTransport(client),
        )
        events = await collect(adapter, True)
    assert attempts == 1
    assert [event.type for event in events] == ["text_delta", "error"]
    assert events[0].text == "partial"
    assert events[-1].error_code == "provider_read_error"
    assert "ReadError" in events[-1].error
    assert SECRET not in repr(events)


@pytest.mark.parametrize("streaming", [False, True])
async def test_route_fallback_classifies_final_failure_without_leaking_either_route(
    monkeypatch, streaming
):
    routes = []

    def proxy_handler(request):
        routes.append("proxy")
        raise httpx.ProxyError(SECRET + "-proxy", request=request)

    def direct_handler(request):
        routes.append("direct")
        raise httpx.ConnectTimeout(SECRET + "-direct", request=request)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(proxy_handler)) as proxy,
        httpx.AsyncClient(transport=httpx.MockTransport(direct_handler)) as direct,
    ):
        transport = HttpxProviderHTTPTransport(proxy)
        monkeypatch.setattr(
            transport,
            "_clients_for_request",
            lambda: (("system proxy", proxy), ("direct network", direct)),
        )
        adapter = OpenAIResponsesProvider(
            base_url="https://fixture.invalid",
            headers=HEADERS,
            transport=transport,
            retry_policy=ProviderRetryPolicy(max_attempts=1),
        )
        events = await collect(adapter, streaming)
    assert routes == ["proxy", "direct"]
    assert events[-1].error_code == "provider_connect_timeout"
    assert SECRET not in repr(events)


def test_unclassified_errors_are_not_classified_from_untrusted_text_or_service_codes():
    failure = request_failure(
        ProviderTransportError(
            f"ReadTimeout: {SECRET}",
            retryable=False,
            service_error_code="provider_read_timeout",
        ),
        policy=ProviderRequestPolicy(),
        redact_errors=True,
    )
    assert str(failure) == "Model request failed."
    assert failure.error_code == "provider_request_failed"
