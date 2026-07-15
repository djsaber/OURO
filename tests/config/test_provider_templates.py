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
    "openai": ("gpt-5.6", {"gpt-5.6", "gpt-5.6-terra", "gpt-5.6-luna"}),
    "anthropic": (
        "claude-sonnet-5",
        {"claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5"},
    ),
    "gemini": (
        "gemini-3.5-flash",
        {"gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-3.1-flash-lite"},
    ),
    "deepseek": ("deepseek-v4-pro", {"deepseek-v4-pro", "deepseek-v4-flash"}),
    "qwen": ("qwen3.7-plus", {"qwen3.7-plus", "qwen3.7-max", "qwen3.6-flash"}),
    "kimi": (
        "kimi-k2.7-code",
        {"kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"},
    ),
    "glm": ("glm-5.2", {"glm-5.2", "glm-5-turbo", "glm-5v-turbo"}),
    "openrouter": ("auto", {"auto", "free"}),
    "mistral": (
        "mistral-medium-3-5",
        {"mistral-medium-3-5", "mistral-small-2603", "mistral-large-2512"},
    ),
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
        assert template.protocol in {"openai-chat-completions", "anthropic-messages"}
        assert {endpoint.endpoint_id for endpoint in template.endpoints} == endpoint_ids
        assert template.verified_on == "2026-07-14"


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
