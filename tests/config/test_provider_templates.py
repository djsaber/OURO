from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from ouro.config import (
    ProviderEndpointConfig,
    ProviderTemplate,
    get_provider_template,
    provider_templates,
)
from ouro.config.provider_models import INTEGRATED_PROVIDER_TYPES
from ouro.core.errors import ConfigError
from ouro.core.reasoning import ReasoningRequest, resolve_reasoning


def _template(**updates: object) -> ProviderTemplate:
    endpoint = ProviderEndpointConfig(endpoint_id="default", model_id="model")
    values = {
        "template_id": "example",
        "display_name": "Example",
        "description": "Example provider template.",
        "provider_type": "custom",
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


def test_builtin_provider_templates_materialize_model_catalogs() -> None:
    templates = {template.template_id: template for template in provider_templates()}
    assert set(templates) == set(INTEGRATED_PROVIDER_TYPES)

    for template_id, template in templates.items():
        assert template.provider_type == template_id
        assert template.protocol in {
            "openai-chat-completions",
            "anthropic-messages",
            "openai-responses",
        }
        assert template.display_name.strip()
        assert template.endpoint().model_id == template.default_endpoint
        assert date.fromisoformat(template.verified_on).isoformat() == template.verified_on
        for endpoint in template.endpoints:
            assert endpoint.display_name
            assert endpoint.endpoint_id == endpoint.model_id
            if template_id in {"deepseek", "doubao", "glm", "kimi", "minimax", "qwen"}:
                assert endpoint.reasoning_text_field == "reasoning_content"


def test_openai_templates_project_api_only_and_subscription_endpoints() -> None:
    api = get_provider_template("openai")
    codex = get_provider_template("openai-codex")
    api_model_ids = {endpoint.model_id for endpoint in api.endpoints}
    codex_model_ids = {endpoint.model_id for endpoint in codex.endpoints}
    assert api.default_endpoint == codex.default_endpoint == "gpt-6.2-sol"
    assert "gpt-6.2-astra" in codex_model_ids
    assert api_model_ids - codex_model_ids == {"gpt-5.5"}
    assert codex_model_ids <= api_model_ids


def test_go_catalog_uses_documented_native_routes() -> None:
    chat = get_provider_template("opencode-go")
    responses = get_provider_template("opencode-go-responses")
    messages = get_provider_template("opencode-go-messages")
    routes = {
        endpoint.model_id: template.template_id
        for template in (chat, responses, messages)
        for endpoint in template.endpoints
    }
    assert len(routes) == sum(len(template.endpoints) for template in (chat, responses, messages))
    assert routes["hy4-flash-free"] == chat.template_id
    assert routes["gpt-6-luna"] == responses.template_id
    assert routes["qwen4-max"] == messages.template_id
    assert chat.endpoint("hy4-flash-free").metadata["provider_input_limit"] == 983_040
    assert chat.endpoint("hy4-flash-free").reasoning_text_field is None
    assert chat.endpoint().reasoning_text_field == "reasoning_content"
    assert messages.endpoint("qwen4-max").max_output_tokens == 32_768


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
    assert openai.capability.default.effort == "medium"
    assert openai.profile is not None
    assert openai.profile.effort["enabled"] == {"reasoning": {"effort": "medium"}}
    assert openai.capability.supported_summaries == ("auto",)
    assert openai.capability.default.summary == "auto"
    assert openai.capability.activation == "always_on"
    assert "disabled" not in openai.profile.effort

    anthropic = templates["anthropic"]
    assert anthropic.endpoint("claude-sonnet-5-5").reasoning.capability.default.effort == "high"
    assert anthropic.endpoint("claude-haiku-5-5").reasoning.capability.default.effort == "medium"

    gemini = templates["gemini"]
    assert gemini.endpoint("gemini-3.8-flash").reasoning.capability.activation == "always_on"
    assert gemini.endpoint("gemini-3.1-pro-preview").reasoning.capability.supported_efforts == (
        "low",
        "medium",
        "high",
    )

    deepseek = templates["deepseek"].endpoint().reasoning
    assert deepseek.capability.supported_efforts == ("low", "high", "max")
    assert deepseek.profile is not None
    assert deepseek.profile.effort["low"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }
    assert deepseek.profile.effort["max"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
    assert deepseek.capability.default.effort == "high"

    qwen = templates["qwen"]
    qwen_max = qwen.endpoint()
    assert qwen_max.model_id == "qwen4-max"
    assert qwen_max.context_window == 1_000_000
    assert qwen_max.max_output_tokens is None
    assert qwen_max.supports_tools is True
    assert qwen_max.supports_image_input is True
    assert qwen_max.supports_streaming is True
    assert qwen_max.supports_structured_output is True
    qwen_reasoning = qwen_max.reasoning
    assert qwen_reasoning.capability.activation == "toggle"
    assert qwen_reasoning.capability.supported_efforts == ("low", "high", "xhigh")
    assert qwen_reasoning.profile is not None
    assert qwen_reasoning.profile.effort["enabled"]["preserve_thinking"] is True
    qwen_flash = qwen.endpoint("qwen3.8-flash")
    assert qwen_flash.context_window == 1_000_000
    assert qwen_flash.max_output_tokens is None
    assert qwen_flash.supports_image_input is True
    assert qwen_flash.supports_structured_output is True
    flash_reasoning = qwen_flash.reasoning
    assert flash_reasoning.capability.activation == "toggle"
    assert flash_reasoning.capability.supported_efforts == ("low", "medium", "xhigh")
    assert flash_reasoning.profile is not None
    assert flash_reasoning.profile.effort["enabled"] == {
        "enable_thinking": True,
        "preserve_thinking": True,
        "reasoning_effort": "xhigh",
    }
    assert flash_reasoning.profile.effort["disabled"]["enable_thinking"] is False
    qwen_plus = qwen.endpoint("qwen3.7-plus")
    assert qwen_plus.context_window == 1_000_000
    assert qwen_plus.max_output_tokens is None
    assert qwen_plus.supports_image_input is True
    assert qwen_plus.supports_structured_output is True
    plus_reasoning = qwen_plus.reasoning
    assert plus_reasoning.capability.activation == "toggle"
    assert plus_reasoning.profile is not None
    assert plus_reasoning.profile.effort["enabled"]["preserve_thinking"] is True

    kimi = templates["kimi"]
    assert kimi.endpoint("kimi-k3").reasoning == kimi.endpoint().reasoning
    k3_reasoning = kimi.endpoint().reasoning
    assert k3_reasoning.capability.activation == "always_on"
    assert k3_reasoning.capability.supported_efforts == ("low", "high", "max")
    assert k3_reasoning.capability.default.effort == "max"
    assert k3_reasoning.profile is not None
    assert k3_reasoning.profile.effort["enabled"] == {"reasoning_effort": "max"}

    glm = templates["glm"]
    glm_53 = glm.endpoint().reasoning
    assert glm_53.capability.activation == "always_on"
    assert glm_53.capability.supported_efforts == ("low", "high", "max")
    assert glm_53.profile is not None
    assert glm_53.profile.effort["enabled"] == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
    glm_53_endpoint = glm.endpoint()
    assert glm_53_endpoint.context_window == 1_000_000
    assert glm_53_endpoint.max_output_tokens is None
    assert glm_53_endpoint.supports_tools is True
    assert glm_53_endpoint.supports_image_input is False
    assert glm_53_endpoint.supports_streaming is True
    assert glm_53_endpoint.supports_structured_output is True
    glm_flash = glm.endpoint("glm-5.3-flash")
    assert glm_flash.context_window == 1_000_000
    assert glm_flash.max_output_tokens is None
    assert glm_flash.supports_tools is True
    assert glm_flash.supports_image_input is True
    assert glm_flash.supports_streaming is True
    assert glm_flash.supports_structured_output is True
    assert glm_flash.reasoning.profile is not None
    assert glm_flash.reasoning.profile.effort["low"] == {
        "thinking": {"type": "enabled", "clear_thinking": False},
        "reasoning_effort": "low",
    }
    legacy_glm = glm.endpoint("glm-5.2").reasoning
    assert legacy_glm.profile is not None
    assert legacy_glm.profile.effort["enabled"] == {
        "thinking": {"type": "enabled", "clear_thinking": False}
    }

    grok = templates["grok"].endpoint()
    assert grok.context_window == 500_000
    assert grok.max_output_tokens is None
    assert grok.supports_tools is True
    assert grok.supports_image_input is True
    assert grok.supports_streaming is True
    assert grok.supports_structured_output is True
    assert grok.reasoning.capability.activation == "always_on"
    assert grok.reasoning.capability.supported_efforts == ("low", "medium", "high", "xhigh")
    assert grok.reasoning.capability.default.effort == "high"
    assert grok.reasoning.profile is not None
    assert grok.reasoning.profile.effort["enabled"] == {"reasoning_effort": "high"}


def test_refreshed_reasoning_controls_match_provider_modes() -> None:
    gemini = get_provider_template("gemini")
    assert gemini.default_endpoint == "gemini-4-hydrogen"
    for endpoint in (gemini.endpoint(), gemini.endpoint("gemini-4-neon")):
        assert endpoint.context_window == 1_048_576
        assert endpoint.supports_audio_input
        reasoning = endpoint.reasoning
        assert reasoning.capability.default.effort == "high"
        assert reasoning.profile is not None
        assert reasoning.profile.effort["xhigh"] == {"reasoning_effort": "xhigh"}
    assert gemini.endpoint("gemini-4-carbon").reasoning.capability.default.effort == "medium"

    doubao = get_provider_template("doubao")
    evolving = doubao.endpoint("doubao-seed-evolving")
    assert evolving.context_window == 1_048_576
    assert evolving.supports_video_input and not evolving.supports_audio_input
    assert doubao.endpoint("doubao-seed-2-1-turbo-260628").context_window == 262_144
    with pytest.raises(ValueError, match="not supported"):
        resolve_reasoning(evolving.reasoning.capability, ReasoningRequest(effort="minimal"))

    mistral = get_provider_template("mistral")
    large = mistral.endpoint().reasoning
    assert resolve_reasoning(large.capability).effort_variant == "low"
    assert large.profile is not None
    assert large.profile.effort["high"] == {"reasoning_effort": "high"}
    small = mistral.endpoint("mistral-small-2603").reasoning
    assert resolve_reasoning(small.capability).effort_variant == "disabled"
    assert small.profile is not None
    assert small.profile.effort["disabled"] == {"reasoning_effort": "none"}


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
