from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

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
    manager.create_provider("glm", provider_type="openai-compatible")
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
    assert "tools" not in payload


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
