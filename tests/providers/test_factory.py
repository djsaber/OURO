from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

import httpx
import pytest

from ouro.config import (
    ProviderConfigManager,
    ProviderRuntimeOptions,
    openai_effort_reasoning,
    reasoning_preset,
)
from ouro.core.context import ContextView
from ouro.core.errors import ConfigError
from ouro.core.messages import ModelMessage
from ouro.core.reasoning import ReasoningRequest
from ouro.providers import (
    AnthropicCompatibleProvider,
    OpenAICompatibleProvider,
    build_runtime_adapter,
)
from ouro.providers.authentication import ProviderRequestPolicy
from ouro.providers.http_transport import HttpxProviderHTTPTransport
from tests.providers.responses_fixtures import message, terminal


@pytest.mark.parametrize(
    ("protocol", "module", "limit_field"),
    [
        ("openai-responses", "openai_responses", "max_output_tokens"),
        ("openai-chat-completions", "openai_compatible", "max_tokens"),
    ],
)
@pytest.mark.parametrize("supports_limit", [False, True])
@pytest.mark.parametrize("billing_mode", ["subscription", "usage_based"])
async def test_output_limit_follows_service_capability_not_billing_or_auth(
    monkeypatch, protocol, module, limit_field, supports_limit, billing_mode
):
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer fixture-key"
        return httpx.Response(
            200,
            json=terminal([message()])["response"]
            if protocol == "openai-responses"
            else {"choices": [{"message": {"role": "assistant", "content": "hello"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            f"ouro.providers.{module}.HttpxProviderHTTPTransport",
            lambda: HttpxProviderHTTPTransport(client),
        )
        adapter = build_runtime_adapter(
            ProviderRuntimeOptions(
                protocol=protocol,
                model="fixture",
                base_url="https://fixture.invalid",
                api_key="fixture-key",
                supports_streaming=False,
                max_output_tokens=1024,
            ),
            request_policy=ProviderRequestPolicy(
                billing_mode=billing_mode, supports_output_limit=supports_limit
            ),
        )
        try:
            for override in (None, 512):
                events = [
                    event
                    async for event in adapter.stream(
                        ContextView(model_messages=[ModelMessage.text(role="user", text="hello")]),
                        max_output_tokens=override,
                    )
                ]
                assert events[-1].type == "done"
                assert not any(event.type == "error" for event in events)
                if supports_limit:
                    assert payloads[-1][limit_field] == (1024 if override is None else override)
                else:
                    assert limit_field not in payloads[-1]
                assert adapter.model.max_output_tokens == 1024
        finally:
            await adapter.provider.close()


def test_messages_rejects_service_without_its_required_output_limit():
    with pytest.raises(ValueError, match="Messages protocol requires a request output limit"):
        build_runtime_adapter(
            ProviderRuntimeOptions(
                protocol="anthropic-messages",
                model="fixture",
                base_url="https://fixture.invalid",
            ),
            request_policy=ProviderRequestPolicy(supports_output_limit=False),
        )


@pytest.mark.parametrize(
    "protocol", ["openai-chat-completions", "anthropic-messages", "openai-responses"]
)
async def test_billing_mode_comes_from_service_declaration_not_static_key(protocol):
    options = ProviderRuntimeOptions(
        protocol=protocol,
        provider_id="custom",
        model="model",
        base_url="https://fixture.invalid",
        api_key="fixture-key",
    )
    unknown = build_runtime_adapter(options)
    declared = build_runtime_adapter(
        options, request_policy=ProviderRequestPolicy(billing_mode="usage_based")
    )
    try:
        assert unknown.model.billing_mode == "unknown"
        assert declared.model.billing_mode == "usage_based"
    finally:
        await unknown.provider.close()
        await declared.provider.close()


class CapturingTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        self.requests.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout": timeout,
            }
        )
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


class AnthropicCapturingTransport(CapturingTransport):
    async def post_json(self, **kwargs) -> dict[str, Any]:
        self.requests.append(dict(kwargs))
        return {"type": "message", "content": [{"type": "text", "text": "ok"}]}


def test_factory_uses_ad_hoc_provider_identity_and_preserves_zero_temperature() -> None:
    adapter = build_runtime_adapter(
        ProviderRuntimeOptions(
            protocol="openai-chat-completions",
            model="test-model",
            base_url="https://example.test/v1/",
            temperature=0,
        )
    )

    assert adapter.model.provider_id == "openai-chat-completions"
    assert adapter.model.temperature == 0
    provider = cast(OpenAICompatibleProvider, adapter.provider)
    assert provider.base_url == "https://example.test/v1"


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (ProviderRuntimeOptions(timeout=0), "timeout"),
        (ProviderRuntimeOptions(context_window=0), "context_window"),
        (ProviderRuntimeOptions(max_output_tokens=0), "max_output_tokens"),
    ],
)
def test_factory_rejects_zero_instead_of_replacing_it_with_a_default(
    options: ProviderRuntimeOptions,
    message: str,
) -> None:
    with pytest.raises(ConfigError, match=message):
        build_runtime_adapter(options)


def test_runtime_credential_override_wins_over_configured_reference(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CONFIGURED_KEY", "configured")
    monkeypatch.setenv("RUNTIME_KEY", "runtime")
    manager = ProviderConfigManager(tmp_path)
    manager.create_provider(
        "glm",
        provider_type="openai-compatible",
        base_url="https://example.test/v1",
        api_key_env="CONFIGURED_KEY",
    )
    manager.upsert_endpoint("glm", "default", model_id="glm-test", make_default=True)

    options = manager.resolve_runtime_options(
        ProviderRuntimeOptions(provider_id="glm", api_key_env="RUNTIME_KEY")
    )
    adapter = build_runtime_adapter(options)

    provider = cast(OpenAICompatibleProvider, adapter.provider)
    assert provider.api_key == "runtime"


@pytest.mark.parametrize(
    "options",
    [
        ProviderRuntimeOptions(reasoning_request=ReasoningRequest(mode="enabled")),
        ProviderRuntimeOptions(
            reasoning=openai_effort_reasoning(
                supported_efforts=("low",),
                default_effort="low",
            ),
            reasoning_request=ReasoningRequest(mode="enabled", effort="high"),
        ),
    ],
)
def test_factory_rejects_unfulfillable_reasoning_requests(
    options: ProviderRuntimeOptions,
) -> None:
    with pytest.raises(ConfigError):
        build_runtime_adapter(options)


@pytest.mark.asyncio
async def test_configured_endpoint_drives_model_metadata_and_provider_request(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    manager.create_provider(
        "glm",
        provider_type="openai-compatible",
        provider_options={"seed": 1, "tool_choice": "none", "top_p": 0.8},
    )
    manager.upsert_endpoint(
        "glm",
        "reasoning",
        model_id="glm-reasoning",
        base_url="https://example.test/v1/",
        api_path="responses/chat",
        temperature=0.2,
        context_window=200_000,
        max_output_tokens=16_000,
        supports_tools=False,
        supports_vision=True,
        supports_streaming=False,
        supports_structured_output=True,
        reasoning=openai_effort_reasoning(
            supported_efforts=("low", "high"),
            default_effort="high",
        ),
        request_options={"seed": 7},
        make_default=True,
    )
    options = manager.resolve_runtime_options(
        ProviderRuntimeOptions(provider_id="glm", endpoint_id="reasoning")
    )
    adapter = build_runtime_adapter(options)
    provider = cast(OpenAICompatibleProvider, adapter.provider)
    transport = CapturingTransport()
    provider.transport = transport

    events = [
        event
        async for event in adapter.stream(
            ContextView(
                system_prompt="System instructions.",
                model_messages=[ModelMessage.text(role="user", text="hello")],
            )
        )
    ]

    assert [event.type for event in events] == ["text_delta", "done"]
    assert adapter.model.endpoint_id == "reasoning"
    assert adapter.model.context_window == 200_000
    assert adapter.model.supports_vision is True
    assert adapter.model.supports_structured_output is True
    payload = transport.requests[0]["payload"]
    assert transport.requests[0]["url"] == "https://example.test/v1/responses/chat"
    assert payload["messages"][0] == {
        "role": "system",
        "content": "System instructions.",
    }
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 16_000
    assert payload["reasoning_effort"] == "high"
    assert payload["stream"] is False
    assert payload["seed"] == 7
    assert payload["tool_choice"] == "none"
    assert payload["top_p"] == 0.8
    assert "tools" not in payload


def test_factory_rejects_request_fields_owned_by_protocol_adapter() -> None:
    with pytest.raises(ConfigError, match="protocol-owned fields: messages, model"):
        build_runtime_adapter(
            ProviderRuntimeOptions(
                protocol="openai-chat-completions",
                model="test-model",
                base_url="https://example.test/v1",
                extra={"messages": [], "model": "wrong-model"},
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol", "reasoning", "intent", "expected"),
    [
        (
            "openai-chat-completions",
            reasoning_preset("thinking-toggle"),
            ReasoningRequest(mode="disabled"),
            {"thinking": {"type": "disabled"}},
        ),
        (
            "anthropic-messages",
            reasoning_preset("anthropic-adaptive-effort"),
            ReasoningRequest(mode="enabled", effort="xhigh"),
            {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "xhigh"},
            },
        ),
    ],
)
async def test_custom_compatible_endpoint_encodes_declared_reasoning_profile(
    protocol,
    reasoning,
    intent,
    expected,
) -> None:
    adapter = build_runtime_adapter(
        ProviderRuntimeOptions(
            protocol=protocol,
            provider_id="custom",
            model="custom-model",
            base_url="https://example.test/v1",
            supports_streaming=False,
            reasoning=reasoning,
            reasoning_request=intent,
        )
    )
    provider = adapter.provider
    transport = (
        AnthropicCapturingTransport()
        if isinstance(provider, AnthropicCompatibleProvider)
        else CapturingTransport()
    )
    provider.transport = transport

    events = [
        event
        async for event in adapter.stream(
            ContextView(model_messages=[ModelMessage.text(role="user", text="hello")])
        )
    ]

    assert [event.type for event in events] == ["text_delta", "done"]
    payload = transport.requests[0]["payload"]
    for key, value in expected.items():
        assert payload[key] == value


@pytest.mark.asyncio
async def test_ad_hoc_openai_compatible_uses_protocol_default_api_path() -> None:
    adapter = build_runtime_adapter(
        ProviderRuntimeOptions(
            protocol="openai-chat-completions",
            model="test-model",
            base_url="https://example.test/v1",
        )
    )

    provider = cast(OpenAICompatibleProvider, adapter.provider)
    assert provider.endpoint == "/chat/completions"


@pytest.mark.asyncio
async def test_ad_hoc_openai_compatible_respects_custom_api_path() -> None:
    adapter = build_runtime_adapter(
        ProviderRuntimeOptions(
            protocol="openai-chat-completions",
            model="test-model",
            base_url="https://example.test/v1",
            api_path="/custom/path",
        )
    )

    provider = cast(OpenAICompatibleProvider, adapter.provider)
    assert provider.endpoint == "/custom/path"


def test_factory_builds_anthropic_compatible_adapter_from_protocol_registry() -> None:
    adapter = build_runtime_adapter(
        ProviderRuntimeOptions(
            protocol="anthropic-messages",
            provider_id="anthropic",
            model="claude-test",
            base_url="https://api.example.test/",
        )
    )

    provider = cast(AnthropicCompatibleProvider, adapter.provider)
    assert provider.provider_id == "anthropic"
    assert provider.base_url == "https://api.example.test"
    assert provider.endpoint == "/v1/messages"
