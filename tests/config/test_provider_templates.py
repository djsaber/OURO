from __future__ import annotations

import pytest
from pydantic import ValidationError

from ouro.config import (
    ProviderEndpointConfig,
    ProviderTemplate,
    provider_templates,
)
from ouro.core.errors import ConfigError

_EXPECTED_ENDPOINTS = {
    "openai-codex": (
        "gpt-5.6-sol",
        {"gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"},
    ),
    "openai": (
        "gpt-5.6-sol",
        {"gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6-astra"},
    ),
    "anthropic": (
        "claude-sonnet-5",
        {"claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5"},
    ),
    "gemini": (
        "gemini-3.5-flash",
        {"gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-3.1-flash-lite"},
    ),
    "deepseek": ("deepseek-v4-pro", {"deepseek-v4-pro", "deepseek-v4-flash"}),
    "qwen": ("qwen3.8-max", {"qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus"}),
    "kimi": (
        "kimi-k3",
        {"kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"},
    ),
    "glm": (
        "glm-5.3",
        {"glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5-turbo", "glm-5v-turbo"},
    ),
    "grok": ("grok-4.6", {"grok-4.6"}),
    "openrouter": ("auto", {"auto", "free"}),
    "mistral": (
        "mistral-medium-3-5",
        {"mistral-medium-3-5", "mistral-small-2603", "mistral-large-2512"},
    ),
}

# Templates re-verified against provider docs after the shared review date.
_EXPECTED_VERIFIED_ON = {
    "openai": "2026-09-19",
    "openai-codex": "2026-09-19",
    "deepseek": "2026-08-27",
    "kimi": "2026-07-17",
    "glm": "2026-08-27",
    "grok": "2026-08-27",
    "qwen": "2026-08-27",
}


def _template(**updates: object) -> ProviderTemplate:
    endpoint = ProviderEndpointConfig(endpoint_id="default", model_id="model")
    values = {
        "template_id": "example",
        "display_name": "Example",
        "description": "Example provider template.",
        "provider_id": "example",
        "provider_type": "openai-compatible",
        "protocol": "openai-chat-completions",
        "credential_env": "EXAMPLE_API_KEY",
        "base_url": "https://example.test/v1",
        "api_path": "/chat/completions",
        "default_endpoint": "default",
        "endpoints": (endpoint,),
        "documentation_url": "https://example.test/docs",
        "verified_on": "2026-07-14",
    }
    values.update(updates)
    return ProviderTemplate.model_validate(values)


def test_builtin_provider_templates_expose_reviewed_endpoint_sets() -> None:
    templates = {template.template_id: template for template in provider_templates()}

    assert set(templates) == set(_EXPECTED_ENDPOINTS)
    for template_id, (default_endpoint, endpoint_ids) in _EXPECTED_ENDPOINTS.items():
        template = templates[template_id]
        assert template.default_endpoint == default_endpoint
        assert template.provider_type == template_id
        assert template.protocol in {
            "openai-chat-completions",
            "anthropic-messages",
            "openai-responses",
        }
        assert {endpoint.endpoint_id for endpoint in template.endpoints} == endpoint_ids
        expected_verified_on = _EXPECTED_VERIFIED_ON.get(template_id, "2026-07-14")
        assert template.verified_on == expected_verified_on


def test_builtin_reasoning_profiles_preserve_model_specific_semantics() -> None:
    templates = {template.template_id: template for template in provider_templates()}

    openai = templates["openai"].endpoint().reasoning
    assert openai.capability.supported_efforts == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert openai.capability.default_effort == "medium"
    assert openai.capability.output == "none"
    assert openai.profile is not None
    assert openai.profile.variants["enabled"] == {"reasoning": {"effort": "medium"}}
    assert openai.profile.variants["disabled"] == {"reasoning": {"effort": "none"}}

    anthropic = templates["anthropic"]
    assert anthropic.endpoint("claude-sonnet-5").reasoning.capability.default.effort == "high"
    assert anthropic.endpoint("claude-opus-4-8").reasoning.capability.continuation == "opaque"
    assert anthropic.endpoint("claude-haiku-4-5").reasoning.capability.supported_efforts == ()

    gemini = templates["gemini"]
    assert gemini.endpoint("gemini-3.5-flash").reasoning.capability.activation == "always_on"
    assert gemini.endpoint("gemini-3.1-pro-preview").reasoning.capability.supported_efforts == (
        "low",
        "medium",
        "high",
    )

    deepseek = templates["deepseek"].endpoint().reasoning
    assert deepseek.capability.supported_efforts == ("low", "high", "max")
    assert deepseek.profile is not None
    assert deepseek.profile.variants["low"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }
    assert deepseek.profile.variants["max"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
    assert deepseek.capability.default_effort == "high"
    assert templates["deepseek"].revision == 3

    qwen = templates["qwen"]
    assert qwen.revision == 4
    qwen_max = qwen.endpoint()
    assert qwen_max.model_id == "qwen3.8-max"
    assert qwen_max.context_window == 1_000_000
    assert qwen_max.max_output_tokens == 131_072
    assert qwen_max.supports_tools is True
    assert qwen_max.supports_vision is True
    assert qwen_max.supports_streaming is True
    assert qwen_max.supports_structured_output is True
    qwen_reasoning = qwen_max.reasoning
    assert qwen_reasoning.capability.activation == "toggle"
    assert qwen_reasoning.profile is not None
    assert qwen_reasoning.profile.variants["enabled"]["preserve_thinking"] is True
    qwen_flash = qwen.endpoint("qwen3.8-flash")
    assert qwen_flash.context_window == 1_000_000
    assert qwen_flash.max_output_tokens == 131_072
    assert qwen_flash.supports_vision is True
    assert qwen_flash.supports_structured_output is True
    flash_reasoning = qwen_flash.reasoning
    assert flash_reasoning.capability.activation == "toggle"
    assert flash_reasoning.capability.supported_efforts == ("low", "medium", "xhigh")
    assert flash_reasoning.profile is not None
    assert flash_reasoning.profile.variants["enabled"] == {
        "enable_thinking": True,
        "preserve_thinking": True,
        "reasoning_effort": "xhigh",
    }
    assert flash_reasoning.profile.variants["disabled"]["enable_thinking"] is False
    qwen_plus = qwen.endpoint("qwen3.7-plus")
    assert qwen_plus.context_window == 1_000_000
    assert qwen_plus.max_output_tokens == 131_072
    assert qwen_plus.supports_vision is True
    assert qwen_plus.supports_structured_output is True
    plus_reasoning = qwen_plus.reasoning
    assert plus_reasoning.capability.activation == "toggle"
    assert plus_reasoning.profile is not None
    assert plus_reasoning.profile.variants["enabled"]["preserve_thinking"] is True

    kimi = templates["kimi"]
    assert kimi.endpoint("kimi-k2.7-code").reasoning.capability.activation == "always_on"
    assert kimi.endpoint("kimi-k2.6").reasoning.capability.activation == "toggle"
    k3_reasoning = kimi.endpoint("kimi-k3").reasoning
    assert k3_reasoning.capability.activation == "always_on"
    assert k3_reasoning.capability.supported_efforts == ("max",)
    assert k3_reasoning.capability.default_effort == "max"
    assert k3_reasoning.profile is not None
    assert k3_reasoning.profile.variants["enabled"] == {"reasoning_effort": "max"}

    glm = templates["glm"]
    assert glm.revision == 3
    glm_53 = glm.endpoint().reasoning
    assert glm_53.capability.activation == "always_on"
    assert glm_53.capability.supported_efforts == ("low", "high", "max")
    assert glm_53.profile is not None
    assert glm_53.profile.variants["enabled"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
    glm_53_endpoint = glm.endpoint()
    assert glm_53_endpoint.context_window == 1_000_000
    assert glm_53_endpoint.max_output_tokens == 128_000
    assert glm_53_endpoint.supports_tools is True
    assert glm_53_endpoint.supports_vision is False
    assert glm_53_endpoint.supports_streaming is True
    assert glm_53_endpoint.supports_structured_output is True
    glm_flash = glm.endpoint("glm-5.3-flash")
    assert glm_flash.context_window == 1_000_000
    assert glm_flash.max_output_tokens == 128_000
    assert glm_flash.supports_tools is True
    assert glm_flash.supports_vision is True
    assert glm_flash.supports_streaming is True
    assert glm_flash.supports_structured_output is True
    assert glm_flash.reasoning.profile is not None
    assert glm_flash.reasoning.profile.variants["low"] == {
        "thinking": {"type": "enabled", "clear_thinking": False},
        "reasoning_effort": "low",
    }
    legacy_glm = glm.endpoint("glm-5.2").reasoning
    assert legacy_glm.profile is not None
    assert legacy_glm.profile.variants["enabled"] == {
        "thinking": {"type": "enabled", "clear_thinking": False}
    }

    grok = templates["grok"].endpoint()
    assert grok.context_window == 500_000
    assert grok.max_output_tokens == 128_000
    assert grok.supports_tools is True
    assert grok.supports_vision is True
    assert grok.supports_streaming is True
    assert grok.supports_structured_output is True
    assert grok.metadata["provider_output_limit"] == "unlimited"
    assert grok.reasoning.capability.activation == "always_on"
    assert grok.reasoning.capability.supported_efforts == ("low", "medium", "high", "xhigh")
    assert grok.reasoning.capability.default_effort == "high"
    assert grok.reasoning.profile is not None
    assert grok.reasoning.profile.variants["enabled"] == {"reasoning_effort": "high"}


def test_provider_template_requires_a_resolvable_default_endpoint() -> None:
    with pytest.raises(ValidationError, match="default endpoint not found"):
        _template(default_endpoint="missing")


def test_provider_template_requires_at_least_one_endpoint() -> None:
    with pytest.raises(ValidationError, match="at least one endpoint"):
        _template(endpoints=())


def test_provider_template_rejects_duplicate_endpoint_ids() -> None:
    endpoint = ProviderEndpointConfig(endpoint_id="default", model_id="model")

    with pytest.raises(ValidationError, match="endpoint ids must be unique"):
        _template(endpoints=(endpoint, endpoint))


def test_provider_template_endpoint_lookup_is_typed() -> None:
    template = _template()

    assert template.endpoint().model_id == "model"
    assert template.endpoint("default").endpoint_id == "default"

    with pytest.raises(ConfigError, match="provider template endpoint not found"):
        template.endpoint("missing")
