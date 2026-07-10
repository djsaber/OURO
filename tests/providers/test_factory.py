from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest

from ouro.config import ProviderConfigManager, ProviderRuntimeOptions
from ouro.core.context import ContextView
from ouro.core.errors import ConfigError
from ouro.core.messages import MessageRecord
from ouro.providers import OpenAICompatibleProvider, build_runtime_adapter


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


def test_factory_uses_ad_hoc_provider_identity_and_preserves_zero_temperature() -> None:
    adapter = build_runtime_adapter(
        ProviderRuntimeOptions(
            provider="openai-compatible",
            model="test-model",
            base_url="https://example.test/v1/",
            temperature=0,
        )
    )

    assert adapter.model.provider_id == "openai_compatible"
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
    manager.upsert_provider(
        "glm",
        provider_type="openai-compatible",
        default_endpoint_id="default",
        default_endpoint_model="glm-test",
        default_endpoint_base_url="https://example.test/v1",
        api_key_env="CONFIGURED_KEY",
    )

    options = manager.resolve_runtime_options(
        ProviderRuntimeOptions(provider_id="glm", api_key_env="RUNTIME_KEY")
    )
    adapter = build_runtime_adapter(options)

    provider = cast(OpenAICompatibleProvider, adapter.provider)
    assert provider.api_key == "runtime"


@pytest.mark.parametrize(
    "options",
    [
        ProviderRuntimeOptions(
            supports_reasoning=False,
            supported_thinking_levels=("low",),
        ),
        ProviderRuntimeOptions(
            supports_reasoning=True,
            supported_thinking_levels=("low", "low"),
        ),
    ],
)
def test_factory_rejects_inconsistent_thinking_capabilities(
    options: ProviderRuntimeOptions,
) -> None:
    with pytest.raises(ConfigError):
        build_runtime_adapter(options)


@pytest.mark.asyncio
async def test_configured_endpoint_drives_model_metadata_and_provider_request(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    manager.upsert_provider("glm", provider_type="openai-compatible")
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
        supports_reasoning=True,
        supported_thinking_levels=("low", "high"),
        default_thinking_level="high",
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
                model_messages=[MessageRecord.text(session_id="ses_1", role="user", text="hello")],
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
async def test_ad_hoc_openai_compatible_uses_kind_default_api_path() -> None:
    adapter = build_runtime_adapter(
        ProviderRuntimeOptions(
            provider="openai-compatible",
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
            provider="openai-compatible",
            model="test-model",
            base_url="https://example.test/v1",
            api_path="/custom/path",
        )
    )

    provider = cast(OpenAICompatibleProvider, adapter.provider)
    assert provider.endpoint == "/custom/path"
