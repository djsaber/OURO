"""Kimi provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import mapped_reasoning, toggle_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_KIMI_REASONING = toggle_reasoning(
    name="kimi-thinking-toggle",
    enabled={"thinking": {"type": "enabled", "keep": "all"}},
    disabled={"thinking": {"type": "disabled"}},
)

_KIMI_ALWAYS_ON_REASONING = mapped_reasoning(
    name="kimi-always-on-thinking",
    activation="always_on",
    default=ReasoningRequest(mode="enabled"),
    effort={"enabled": {}},
)

# Kimi K3 always reasons and uses top-level reasoning_effort.
_KIMI_K3_REASONING = mapped_reasoning(
    name="kimi-k3-reasoning-effort",
    activation="always_on",
    supported_efforts=("low", "high", "max"),
    default=ReasoningRequest(mode="enabled", effort="max"),
    effort={
        "enabled": {"reasoning_effort": "max"},
        **{effort: {"reasoning_effort": effort} for effort in ("low", "high", "max")},
    },
)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="kimi",
        display_name="Moonshot AI Kimi",
        description="Kimi's Chat Completions API with the K3 flagship and K2 Agent models.",
        provider_type="kimi",
        protocol="openai-chat-completions",
        credential_env="MOONSHOT_API_KEY",
        base_url="https://api.moonshot.ai/v1",
        api_path="/chat/completions",
        default_endpoint="kimi-k3",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="kimi-k3",
                display_name="Kimi K3",
                model_id="kimi-k3",
                context_window=1_048_576,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_K3_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="kimi-k2.7-code",
                display_name="Kimi K2.7 Code",
                model_id="kimi-k2.7-code",
                context_window=262_144,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_ALWAYS_ON_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="kimi-k2.7-code-highspeed",
                display_name="Kimi K2.7 Code Highspeed",
                model_id="kimi-k2.7-code-highspeed",
                context_window=262_144,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_ALWAYS_ON_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="kimi-k2.6",
                display_name="Kimi K2.6",
                model_id="kimi-k2.6",
                context_window=262_144,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_KIMI_REASONING,
            ),
        ),
        documentation_url="https://platform.kimi.com/docs/models",
        verified_on=_VERIFIED_ON,
        revision=9,
    ),
)
