from __future__ import annotations

import asyncio

import httpx
import pytest

from ouro.config.provider_headers import parse_provider_headers
from ouro.config.provider_models import ProviderRuntimeOptions
from ouro.core.errors import ConfigError
from ouro.core.messages import ModelMessage
from ouro.core.provider import ModelMetadata, ProviderRetryPolicy
from ouro.core.request_context import ProviderRequestContext
from ouro.providers.factory import build_runtime_adapter
from ouro.providers.headers import ProviderHeaderResolver
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from tests.providers.test_request_authentication import PROVIDERS, response


@pytest.mark.parametrize("provider_class", PROVIDERS)
async def test_registered_context_fields_use_actual_route_and_explicit_call_facts(provider_class):
    fields = ("session.id", "run.id", "provider.id", "endpoint.id", "model.id", "request.purpose")
    received = []

    def respond(request):
        received.append(request.headers)
        return response(provider_class, False)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = provider_class(
            base_url="https://model.test",
            transport=HttpxProviderHTTPTransport(client),
            headers=parse_provider_headers(
                {f"X-{key.replace('.', '-')}": {"source": "context", "key": key} for key in fields}
            ),
        )
        events = [
            event
            async for event in provider.stream(
                model=ModelMetadata(
                    model_id="selected-model",
                    provider_id="selected-provider",
                    endpoint_id="selected-endpoint",
                    supports_streaming=False,
                ),
                messages=[],
                request_context=ProviderRequestContext(
                    {
                        "session.id": "session",
                        "run.id": "run",
                        "request.purpose": "chat",
                        "model.id": "stale-model",
                        "provider.id": "stale-provider",
                        "endpoint.id": "stale-endpoint",
                    }
                ),
            )
        ]
    assert events[-1].type == "done"
    assert {key: received[0][f"x-{key.replace('.', '-')}"] for key in fields} == {
        "session.id": "session",
        "run.id": "run",
        "request.purpose": "chat",
        "model.id": "selected-model",
        "provider.id": "selected-provider",
        "endpoint.id": "selected-endpoint",
    }


async def test_configured_headers_reach_http_without_shared_session_state(monkeypatch):
    monkeypatch.setenv("OURO_TEST_TENANT", "tenant-value")
    received = []

    async def respond(request):
        await asyncio.sleep(0)
        received.append(request.headers)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    adapter = build_runtime_adapter(
        ProviderRuntimeOptions(
            protocol="openai-chat-completions",
            model="test",
            base_url="https://example.test",
            supports_streaming=False,
            headers=parse_provider_headers(
                {
                    "X-Client": {"source": "literal", "value": "ouro-test"},
                    "X-Tenant": {"source": "env", "name": "OURO_TEST_TENANT"},
                    "X-Conversation": {"source": "context", "key": "session.id"},
                }
            ),
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.provider.transport = HttpxProviderHTTPTransport(client)

        async def send(session_id):
            return [
                event
                async for event in adapter.provider.stream(
                    model=adapter.model,
                    messages=[ModelMessage.text(role="user", text="hello")],
                    request_context=ProviderRequestContext({"session.id": session_id}),
                )
            ]

        await asyncio.gather(send("one"), send("two"))
        await send("one")
    assert [headers["x-conversation"] for headers in received] == ["one", "two", "one"]
    assert all(headers["x-client"] == "ouro-test" for headers in received)
    assert all(headers["x-tenant"] == "tenant-value" for headers in received)
    assert all(headers["user-agent"].startswith("ouro/") for headers in received)


@pytest.mark.parametrize("provider_class", PROVIDERS)
@pytest.mark.parametrize("streaming", [False, True])
async def test_all_protocols_checks_and_retries_share_resolved_headers(
    provider_class, streaming, monkeypatch
):
    monkeypatch.setenv("OURO_TEST_TENANT", "initial")
    received = []

    def respond(request):
        received.append(request.headers)
        if len(received) == 1:
            monkeypatch.setenv("OURO_TEST_TENANT", "changed")
            return httpx.Response(503, text="private upstream details")
        return response(provider_class, streaming if len(received) < 3 else False)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = provider_class(
            base_url="https://model.test",
            transport=HttpxProviderHTTPTransport(client),
            retry_policy=ProviderRetryPolicy(initial_backoff_seconds=0, max_backoff_seconds=0),
            headers=parse_provider_headers(
                {
                    "X-Context": {"source": "context", "key": "session.id"},
                    "X-Tenant": {"source": "env", "name": "OURO_TEST_TENANT"},
                    "User-Agent": {"source": "literal", "value": "custom-client/1.0"},
                }
            ),
        )
        model = ModelMetadata(model_id="model", provider_id="service", supports_streaming=streaming)
        events = [
            event
            async for event in provider.stream(
                model=model,
                messages=[],
                request_context=ProviderRequestContext({"session.id": "conversation"}),
            )
        ]
        assert events[-1].type == "done"
        assert events[0].type == "retry_scheduled"
        assert "private upstream details" not in events[0].retry.reason
        result = await provider.check_connection(
            model.model_copy(update={"supports_streaming": False}),
            request_context=ProviderRequestContext({"session.id": "probe"}),
        )
        assert result.ok
    assert [item["x-context"] for item in received] == ["conversation", "conversation", "probe"]
    assert [item["x-tenant"] for item in received] == ["initial", "initial", "changed"]
    assert all(item["user-agent"] == "custom-client/1.0" for item in received)


@pytest.mark.parametrize("provider_class", PROVIDERS)
@pytest.mark.parametrize(
    "source",
    [
        {"source": "context", "key": "session.id"},
        {"source": "env", "name": "OURO_TEST_MISSING_HEADER"},
    ],
)
async def test_missing_values_fail_before_authentication_or_http(
    provider_class, source, monkeypatch
):
    monkeypatch.delenv("OURO_TEST_MISSING_HEADER", raising=False)

    class UnusedAuthenticator:
        async def acquire(self, url):
            pytest.fail("missing header input must fail before authentication")

    provider = provider_class(
        base_url="https://model.test",
        authenticator=UnusedAuthenticator(),
        headers=parse_provider_headers({"X-Value": source}),
    )
    try:
        with pytest.raises(ConfigError, match="unavailable"):
            [
                event
                async for event in provider.stream(
                    model=ModelMetadata(model_id="model", provider_id="test"), messages=[]
                )
            ]
    finally:
        await provider.close()


@pytest.mark.parametrize("value", ["", "private\nvalue", "private\x00value", "中文", " private"])
def test_resolved_values_are_validated_without_echo(value):
    resolver = ProviderHeaderResolver(
        parse_provider_headers({"X-Value": {"source": "env", "name": "TEST"}}),
        environ={"TEST": value},
    )
    with pytest.raises(ConfigError, match="invalid value") as error:
        resolver.resolve(None, defaults={})
    assert "private" not in str(error.value)


@pytest.mark.parametrize("provider_class", PROVIDERS)
async def test_authentication_conflict_is_case_insensitive_and_sends_no_request(provider_class):
    from ouro.auth.request_auth import StaticRequestAuthenticator

    def respond(request):
        pytest.fail("conflicting headers must not be sent")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = provider_class(
            base_url="https://model.test",
            transport=HttpxProviderHTTPTransport(client),
            authenticator=StaticRequestAuthenticator({"X-Identity": "auth-secret"}),
            headers=parse_provider_headers(
                {"x-IDENTITY": {"source": "literal", "value": "custom-secret"}}
            ),
        )
        events = [
            event
            async for event in provider.stream(
                model=ModelMetadata(model_id="model", provider_id="test"), messages=[]
            )
        ]
    assert events[-1].type == "error"
    assert "authentication" in events[-1].error
    assert "secret" not in events[-1].error


@pytest.mark.parametrize("provider_class", PROVIDERS)
@pytest.mark.parametrize("status", [200, 400])
async def test_upstream_error_cannot_echo_custom_header_values(provider_class, status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, json={"error": {"message": "header-private-value"}}
            )
        )
    ) as client:
        provider = provider_class(
            base_url="https://model.test",
            transport=HttpxProviderHTTPTransport(client),
            headers=parse_provider_headers(
                {"X-Private": {"source": "literal", "value": "header-private-value"}}
            ),
        )
        events = [
            event
            async for event in provider.stream(
                model=ModelMetadata(model_id="model", provider_id="test", supports_streaming=False),
                messages=[],
            )
        ]
    assert events[-1].type == "error"
    assert "header-private-value" not in events[-1].model_dump_json()
