from __future__ import annotations

from pydantic import ConfigDict, field_validator, model_validator

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_models import (
    ProviderProtocol,
    ProviderType,
    resolve_provider_protocol,
)
from ouro.config.reasoning import (
    mapped_reasoning,
    openai_effort_reasoning,
    toggle_reasoning,
)
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.core.reasoning import ReasoningRequest


class ProviderTemplate(OuroModel):
    """A reviewed provider configuration preset with callable model endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    template_id: str
    display_name: str
    description: str
    provider_id: str
    provider_type: ProviderType
    protocol: ProviderProtocol
    credential_env: str
    base_url: str
    api_path: str
    default_endpoint: str
    endpoints: tuple[ProviderEndpointConfig, ...]
    documentation_url: str
    verified_on: str

    @field_validator("template_id", "provider_id", "credential_env", "default_endpoint")
    @classmethod
    def _require_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be empty")
        return normalized

    @model_validator(mode="after")
    def _validate_endpoint_registry(self) -> ProviderTemplate:
        resolve_provider_protocol(self.provider_type, self.protocol)
        endpoint_ids = [endpoint.endpoint_id for endpoint in self.endpoints]
        if not endpoint_ids:
            raise ValueError("provider template must contain at least one endpoint")
        if len(set(endpoint_ids)) != len(endpoint_ids):
            raise ValueError("provider template endpoint ids must be unique")
        if self.default_endpoint not in endpoint_ids:
            raise ValueError(f"default endpoint not found: {self.default_endpoint}")
        return self

    def endpoint(self, endpoint_id: str | None = None) -> ProviderEndpointConfig:
        target = endpoint_id or self.default_endpoint
        endpoint = next(
            (item for item in self.endpoints if item.endpoint_id == target),
            None,
        )
        if endpoint is None:  # Protected by validation for the default lookup.
            raise ConfigError(f"provider template endpoint not found: {self.template_id}/{target}")
        return endpoint


_VERIFIED_ON = "2026-07-14"


_OPENAI_REASONING = openai_effort_reasoning(
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
)
_GEMINI_FLASH_REASONING = openai_effort_reasoning(
    activation="always_on",
    supported_efforts=("minimal", "low", "medium", "high"),
    default_effort="medium",
)
_GEMINI_PRO_REASONING = openai_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high"),
    default_effort="high",
)
_GEMINI_FLASH_LITE_REASONING = openai_effort_reasoning(
    activation="always_on",
    supported_efforts=("minimal", "low", "medium", "high"),
    default_effort="minimal",
)
_DEEPSEEK_REASONING = mapped_reasoning(
    name="deepseek-thinking-effort",
    activation="toggle",
    supported_efforts=("high", "max"),
    default=ReasoningRequest(mode="enabled", effort="high"),
    output="text",
    continuation="text",
    variants={
        "disabled": {"thinking": {"type": "disabled"}},
        "enabled": {"thinking": {"type": "enabled"}},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        "max": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
    },
)
_QWEN_REASONING = toggle_reasoning(
    name="qwen-thinking-toggle",
    enabled={"enable_thinking": True, "preserve_thinking": True},
    disabled={"enable_thinking": False, "preserve_thinking": False},
)
_KIMI_REASONING = toggle_reasoning(
    name="kimi-thinking-toggle",
    enabled={"thinking": {"type": "enabled", "keep": "all"}},
    disabled={"thinking": {"type": "disabled"}},
)
_KIMI_ALWAYS_ON_REASONING = mapped_reasoning(
    name="kimi-always-on-thinking",
    activation="always_on",
    default=ReasoningRequest(mode="enabled"),
    output="text",
    continuation="text",
    variants={"enabled": {}},
)
# Kimi K3 always reasons; the only reasoning control is the top-level
# reasoning_effort field, which currently accepts just "max".
_KIMI_K3_REASONING = mapped_reasoning(
    name="kimi-k3-reasoning-effort",
    activation="always_on",
    supported_efforts=("max",),
    default=ReasoningRequest(mode="enabled", effort="max"),
    output="text",
    continuation="text",
    variants={
        "enabled": {"reasoning_effort": "max"},
        "max": {"reasoning_effort": "max"},
    },
)
_GLM_REASONING = toggle_reasoning(
    name="glm-thinking-toggle",
    enabled={"thinking": {"type": "enabled", "clear_thinking": False}},
    disabled={"thinking": {"type": "disabled"}},
)
_ANTHROPIC_SONNET_REASONING = mapped_reasoning(
    name="anthropic-default-adaptive-effort",
    activation="toggle",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
    default=ReasoningRequest(mode="enabled", effort="high"),
    output="summary",
    continuation="opaque",
    variants={
        "disabled": {"thinking": {"type": "disabled"}},
        "enabled": {},
        **{
            effort: {"output_config": {"effort": effort}}
            for effort in ("low", "medium", "high", "xhigh", "max")
        },
    },
)
_ANTHROPIC_ADAPTIVE_REASONING = mapped_reasoning(
    name="anthropic-adaptive-effort",
    activation="toggle",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
    default=ReasoningRequest(mode="enabled", effort="high"),
    output="summary",
    continuation="opaque",
    variants={
        "disabled": {"thinking": {"type": "disabled"}},
        "enabled": {"thinking": {"type": "adaptive"}},
        "low": {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "low"},
        },
        "medium": {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium"},
        },
        "high": {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        },
        "xhigh": {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "xhigh"},
        },
        "max": {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "max"},
        },
    },
)
_ANTHROPIC_MANUAL_REASONING = toggle_reasoning(
    name="anthropic-manual-thinking",
    enabled={"thinking": {"type": "enabled", "budget_tokens": 8192}},
    disabled={"thinking": {"type": "disabled"}},
    default_enabled=False,
    output="summary",
    continuation="opaque",
)


_PROVIDER_TEMPLATES = (
    ProviderTemplate(
        template_id="openai",
        display_name="OpenAI",
        description="OpenAI Chat Completions with current GPT-5.6 quality tiers.",
        provider_id="openai",
        provider_type="openai",
        protocol="openai-chat-completions",
        credential_env="OPENAI_API_KEY",
        base_url="https://api.openai.com/v1",
        api_path="/chat/completions",
        default_endpoint="gpt-5.6",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="gpt-5.6",
                model_id="gpt-5.6",
                context_window=1_050_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_OPENAI_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="gpt-5.6-terra",
                model_id="gpt-5.6-terra",
                context_window=1_050_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_OPENAI_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="gpt-5.6-luna",
                model_id="gpt-5.6-luna",
                context_window=1_050_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_OPENAI_REASONING,
            ),
        ),
        documentation_url="https://developers.openai.com/api/docs/models",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="anthropic",
        display_name="Anthropic",
        description="Anthropic Messages API with current Claude production models.",
        provider_id="anthropic",
        provider_type="anthropic",
        protocol="anthropic-messages",
        credential_env="ANTHROPIC_API_KEY",
        base_url="https://api.anthropic.com",
        api_path="/v1/messages",
        default_endpoint="claude-sonnet-5",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="claude-sonnet-5",
                model_id="claude-sonnet-5",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_SONNET_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="claude-opus-4-8",
                model_id="claude-opus-4-8",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_ADAPTIVE_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="claude-haiku-4-5",
                model_id="claude-haiku-4-5",
                context_window=200_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_MANUAL_REASONING,
            ),
        ),
        documentation_url="https://platform.claude.com/docs/en/about-claude/models/overview",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="gemini",
        display_name="Google Gemini",
        description="Gemini through Google's official OpenAI-compatible endpoint.",
        provider_id="gemini",
        provider_type="gemini",
        protocol="openai-chat-completions",
        credential_env="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_path="/chat/completions",
        default_endpoint="gemini-3.5-flash",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="gemini-3.5-flash",
                model_id="gemini-3.5-flash",
                context_window=1_048_576,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GEMINI_FLASH_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="gemini-3.1-pro-preview",
                model_id="gemini-3.1-pro-preview",
                context_window=1_048_576,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GEMINI_PRO_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="gemini-3.1-flash-lite",
                model_id="gemini-3.1-flash-lite",
                context_window=1_048_576,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GEMINI_FLASH_LITE_REASONING,
            ),
        ),
        documentation_url="https://ai.google.dev/gemini-api/docs/models",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="deepseek",
        display_name="DeepSeek",
        description="DeepSeek's OpenAI-compatible API with current V4 models.",
        provider_id="deepseek",
        provider_type="deepseek",
        protocol="openai-chat-completions",
        credential_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        api_path="/chat/completions",
        default_endpoint="deepseek-v4-pro",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="deepseek-v4-pro",
                model_id="deepseek-v4-pro",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_DEEPSEEK_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="deepseek-v4-flash",
                model_id="deepseek-v4-flash",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_DEEPSEEK_REASONING,
            ),
        ),
        documentation_url="https://api-docs.deepseek.com/quick_start/pricing/",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="qwen",
        display_name="Alibaba Cloud Qwen",
        description="DashScope's OpenAI-compatible China endpoint with current Qwen models.",
        provider_id="qwen",
        provider_type="qwen",
        protocol="openai-chat-completions",
        credential_env="DASHSCOPE_API_KEY",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_path="/chat/completions",
        default_endpoint="qwen3.7-plus",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="qwen3.7-plus",
                model_id="qwen3.7-plus",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_QWEN_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="qwen3.7-max",
                model_id="qwen3.7-max",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=False,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_QWEN_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="qwen3.6-flash",
                model_id="qwen3.6-flash",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_QWEN_REASONING,
            ),
        ),
        documentation_url="https://help.aliyun.com/zh/model-studio/text-generation-model",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="kimi",
        display_name="Moonshot AI Kimi",
        description="Kimi's OpenAI-compatible API with the K3 flagship and K2 Agent models.",
        provider_id="kimi",
        provider_type="kimi",
        protocol="openai-chat-completions",
        credential_env="MOONSHOT_API_KEY",
        base_url="https://api.moonshot.ai/v1",
        api_path="/chat/completions",
        default_endpoint="kimi-k3",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="kimi-k3",
                model_id="kimi-k3",
                context_window=1_048_576,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_K3_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="kimi-k2.7-code",
                model_id="kimi-k2.7-code",
                context_window=262_144,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_ALWAYS_ON_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="kimi-k2.7-code-highspeed",
                model_id="kimi-k2.7-code-highspeed",
                context_window=262_144,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_ALWAYS_ON_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="kimi-k2.6",
                model_id="kimi-k2.6",
                context_window=262_144,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_REASONING,
            ),
        ),
        documentation_url="https://platform.kimi.com/docs/guide/use-thinking-effort",
        verified_on="2026-07-17",
    ),
    ProviderTemplate(
        template_id="glm",
        display_name="Zhipu GLM",
        description="Zhipu's OpenAI-compatible API with current text and vision models.",
        provider_id="glm",
        provider_type="glm",
        protocol="openai-chat-completions",
        credential_env="ZAI_API_KEY",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_path="/chat/completions",
        default_endpoint="glm-5.2",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="glm-5.2",
                model_id="glm-5.2",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GLM_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="glm-5-turbo",
                model_id="glm-5-turbo",
                context_window=200_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GLM_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="glm-5v-turbo",
                model_id="glm-5v-turbo",
                context_window=200_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_GLM_REASONING,
            ),
        ),
        documentation_url="https://docs.bigmodel.cn/cn/guide/start/model-overview",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="openrouter",
        display_name="OpenRouter",
        description="OpenRouter's automatic paid and free model routers.",
        provider_id="openrouter",
        provider_type="openrouter",
        protocol="openai-chat-completions",
        credential_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        api_path="/chat/completions",
        default_endpoint="auto",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="auto",
                model_id="openrouter/auto",
                context_window=128_000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
            ),
            ProviderEndpointConfig(
                endpoint_id="free",
                model_id="openrouter/free",
                context_window=200_000,
                max_output_tokens=8192,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
            ),
        ),
        documentation_url="https://openrouter.ai/docs/guides/routing/routers/auto-router",
        verified_on=_VERIFIED_ON,
    ),
    ProviderTemplate(
        template_id="mistral",
        display_name="Mistral AI",
        description="Mistral's Chat Completions API with current general models.",
        provider_id="mistral",
        provider_type="mistral",
        protocol="openai-chat-completions",
        credential_env="MISTRAL_API_KEY",
        base_url="https://api.mistral.ai/v1",
        api_path="/chat/completions",
        default_endpoint="mistral-medium-3-5",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="mistral-medium-3-5",
                model_id="mistral-medium-3-5",
                context_window=256_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
            ),
            ProviderEndpointConfig(
                endpoint_id="mistral-small-2603",
                model_id="mistral-small-2603",
                context_window=256_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
            ),
            ProviderEndpointConfig(
                endpoint_id="mistral-large-2512",
                model_id="mistral-large-2512",
                context_window=256_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_vision=True,
                supports_streaming=True,
                supports_structured_output=True,
            ),
        ),
        documentation_url="https://docs.mistral.ai/models/model-selection-guide",
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
