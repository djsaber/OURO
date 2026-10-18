"""Deepseek provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import mapped_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_DEEPSEEK_REASONING = mapped_reasoning(
    name="deepseek-thinking-effort",
    activation="toggle",
    supported_efforts=("low", "high", "max"),
    default=ReasoningRequest(mode="enabled", effort="high"),
    effort={
        "disabled": {"thinking": {"type": "disabled"}},
        "enabled": {"thinking": {"type": "enabled"}},
        "low": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "high": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        "max": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
    },
)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="deepseek",
        display_name="DeepSeek",
        description=(
            "DeepSeek's Chat Completions API with V4 Pro and V4.1 Flash (deepseek-flash). "
            "No request output limit is configured."
        ),
        provider_type="deepseek",
        protocol="openai-chat-completions",
        credential_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        api_path="/chat/completions",
        default_endpoint="deepseek-v4-pro",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="deepseek-v4-pro",
                display_name="DeepSeek V4 Pro",
                model_id="deepseek-v4-pro",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_DEEPSEEK_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="deepseek-flash",
                display_name="DeepSeek V4.1 Flash",
                model_id="deepseek-flash",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_DEEPSEEK_REASONING,
            ),
        ),
        documentation_url="https://api-docs.deepseek.com/quick_start/pricing/",
        verified_on=_VERIFIED_ON,
        revision=10,
    ),
)
