from __future__ import annotations

from pydantic import ConfigDict, field_validator

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_models import ProviderKind
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError


class ProviderTemplate(OuroModel):
    """A reviewed provider and default endpoint configuration preset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    template_id: str
    display_name: str
    description: str
    provider_id: str
    provider_type: ProviderKind
    credential_env: str
    base_url: str
    api_path: str
    endpoint: ProviderEndpointConfig
    documentation_url: str
    verified_on: str

    @field_validator("template_id", "provider_id", "credential_env")
    @classmethod
    def _require_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized


_VERIFIED_ON = "2026-07-14"


_PROVIDER_TEMPLATES = (
    ProviderTemplate(
        template_id="openai",
        display_name="OpenAI",
        description="OpenAI Chat Completions with the current flagship model.",
        provider_id="openai",
        provider_type="openai-compatible",
        credential_env="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
        api_path="/chat/completions",
        endpoint=ProviderEndpointConfig(
            endpoint_id="gpt-5.6",
            model_id="gpt-5.6",
            context_window=1_050_000,
            max_output_tokens=32_768,
            supports_tools=True,
            supports_vision=True,
            supports_streaming=True,
            supports_structured_output=True,
            supports_reasoning=True,
            supported_thinking_levels=(
                "off",
                "low",
                "medium",
                "high",
                "xhigh",
                "max",
            ),
            default_thinking_level="medium",
        ),
        documentation_url="https://developers.openai.com/api/docs/models/gpt-5.6-sol",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="anthropic",
        display_name="Anthropic",
        description="Anthropic Messages API with Claude Sonnet 5.",
        provider_id="anthropic",
        provider_type="anthropic-compatible",
        credential_env="ANTHROPIC_API_KEY",
        base_url="https://api.anthropic.com",
        api_path="/v1/messages",
        endpoint=ProviderEndpointConfig(
            endpoint_id="claude-sonnet-5",
            model_id="claude-sonnet-5",
            context_window=1_000_000,
            max_output_tokens=32_768,
            supports_tools=True,
            supports_vision=True,
            supports_streaming=True,
            supports_structured_output=False,
            supports_reasoning=False,
            request_options={"thinking": {"type": "disabled"}},
        ),
        documentation_url=("https://platform.claude.com/docs/en/about-claude/models/overview"),
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="gemini",
        display_name="Google Gemini",
        description="Gemini through Google's official OpenAI-compatible endpoint.",
        provider_id="gemini",
        provider_type="openai-compatible",
        credential_env="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_path="/chat/completions",
        endpoint=ProviderEndpointConfig(
            endpoint_id="gemini-3.5-flash",
            model_id="gemini-3.5-flash",
            context_window=1_000_000,
            max_output_tokens=32_768,
            supports_tools=True,
            supports_vision=True,
            supports_streaming=True,
            supports_structured_output=True,
            supports_reasoning=True,
            supported_thinking_levels=("minimal", "low", "medium", "high"),
            default_thinking_level="medium",
        ),
        documentation_url="https://ai.google.dev/gemini-api/docs/openai",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="deepseek",
        display_name="DeepSeek",
        description="DeepSeek's OpenAI-compatible API with the V4 Pro model.",
        provider_id="deepseek",
        provider_type="openai-compatible",
        credential_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        api_path="/chat/completions",
        endpoint=ProviderEndpointConfig(
            endpoint_id="deepseek-v4-pro",
            model_id="deepseek-v4-pro",
            context_window=1_000_000,
            max_output_tokens=32_768,
            supports_tools=True,
            supports_vision=False,
            supports_streaming=True,
            supports_structured_output=True,
            supports_reasoning=True,
            request_options={"thinking": {"type": "enabled"}},
        ),
        documentation_url="https://api-docs.deepseek.com/quick_start/pricing/",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="qwen",
        display_name="Alibaba Cloud Qwen",
        description="DashScope's OpenAI-compatible China endpoint with Qwen 3.7 Plus.",
        provider_id="qwen",
        provider_type="openai-compatible",
        credential_env="DASHSCOPE_API_KEY",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_path="/chat/completions",
        endpoint=ProviderEndpointConfig(
            endpoint_id="qwen3.7-plus",
            model_id="qwen3.7-plus",
            context_window=1_000_000,
            max_output_tokens=32_768,
            supports_tools=True,
            supports_vision=False,
            supports_streaming=True,
            supports_structured_output=True,
            supports_reasoning=True,
            request_options={"enable_thinking": True},
        ),
        documentation_url=("https://help.aliyun.com/zh/model-studio/text-generation-model"),
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="glm",
        display_name="Zhipu GLM",
        description="Zhipu's OpenAI-compatible API with GLM-5.1.",
        provider_id="glm",
        provider_type="openai-compatible",
        credential_env="ZAI_API_KEY",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_path="/chat/completions",
        endpoint=ProviderEndpointConfig(
            endpoint_id="glm-5.1",
            model_id="glm-5.1",
            context_window=200_000,
            max_output_tokens=32_768,
            supports_tools=True,
            supports_vision=False,
            supports_streaming=True,
            supports_structured_output=True,
            supports_reasoning=True,
            request_options={"thinking": {"type": "enabled"}},
        ),
        documentation_url="https://docs.bigmodel.cn/cn/guide/models/text/glm-5.1",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="openrouter",
        display_name="OpenRouter",
        description="OpenRouter Auto Router with conservative portable capabilities.",
        provider_id="openrouter",
        provider_type="openai-compatible",
        credential_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        api_path="/chat/completions",
        endpoint=ProviderEndpointConfig(
            endpoint_id="auto",
            model_id="openrouter/auto",
            context_window=128_000,
            max_output_tokens=8192,
            supports_tools=True,
            supports_vision=False,
            supports_streaming=True,
            supports_structured_output=False,
            supports_reasoning=False,
        ),
        documentation_url=("https://openrouter.ai/docs/guides/routing/routers/auto-router"),
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="mistral",
        display_name="Mistral AI",
        description="Mistral's Chat Completions API with Mistral Medium 3.5.",
        provider_id="mistral",
        provider_type="openai-compatible",
        credential_env="MISTRAL_API_KEY",
        base_url="https://api.mistral.ai/v1",
        api_path="/chat/completions",
        endpoint=ProviderEndpointConfig(
            endpoint_id="mistral-medium-3-5",
            model_id="mistral-medium-3-5",
            context_window=256_000,
            max_output_tokens=32_768,
            supports_tools=True,
            supports_vision=True,
            supports_streaming=True,
            supports_structured_output=True,
            supports_reasoning=True,
            supported_thinking_levels=("off", "high"),
            default_thinking_level="high",
        ),
        documentation_url=("https://docs.mistral.ai/models/model-cards/mistral-medium-3-5-26-04"),
        verified_on=_VERIFIED_ON,
    ),
)

_TEMPLATES_BY_ID = {template.template_id: template for template in _PROVIDER_TEMPLATES}


def provider_templates() -> tuple[ProviderTemplate, ...]:
    return _PROVIDER_TEMPLATES


def get_provider_template(template_id: str) -> ProviderTemplate:
    normalized = template_id.strip().casefold()
    template = _TEMPLATES_BY_ID.get(normalized)
    if template is None:
        available = ", ".join(sorted(_TEMPLATES_BY_ID))
        raise ConfigError(f"provider template not found: {template_id}; available: {available}")
    return template
