"""Anthropic provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import mapped_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_ANTHROPIC_SONNET_REASONING = mapped_reasoning(
    name="anthropic-always-on-adaptive-effort-high",
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
    default_effort="high",
    default=ReasoningRequest(mode="enabled", effort="high"),
    output="summary",
    continuation="opaque",
    variants={
        "enabled": {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}},
        **{
            effort: {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
            for effort in ("low", "medium", "high", "xhigh", "max")
        },
    },
)

_ANTHROPIC_OPUS_REASONING = mapped_reasoning(
    name="anthropic-always-on-adaptive-effort-medium",
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
    default_effort="medium",
    default=ReasoningRequest(mode="enabled", effort="medium"),
    output="summary",
    continuation="opaque",
    variants={
        "enabled": {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}},
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

_ANTHROPIC_HAIKU_REASONING = mapped_reasoning(
    name="anthropic-adaptive-effort-medium",
    activation="toggle",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
    default_effort="medium",
    default=ReasoningRequest(mode="enabled", effort="medium"),
    output="summary",
    continuation="opaque",
    variants={
        "disabled": {"thinking": {"type": "disabled"}, "output_config": {"effort": "medium"}},
        "enabled": {"thinking": {"type": "adaptive"}, "output_config": {"effort": "medium"}},
        **{
            effort: {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}
            for effort in ("low", "medium", "high", "xhigh", "max")
        },
    },
)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="anthropic",
        display_name="Anthropic",
        description="Anthropic Messages API with current Claude production models.",
        provider_type="anthropic",
        protocol="anthropic-messages",
        credential_env="ANTHROPIC_API_KEY",
        base_url="https://api.anthropic.com",
        api_path="/v1/messages",
        default_endpoint="claude-sonnet-5-5",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="claude-fable-5-1",
                display_name="Claude Fable 5.1",
                model_id="claude-fable-5-1",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_SONNET_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="claude-sonnet-5-5",
                display_name="Claude Sonnet 5.5",
                model_id="claude-sonnet-5-5",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_SONNET_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="claude-opus-5-5",
                display_name="Claude Opus 5.5",
                model_id="claude-opus-5-5",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_OPUS_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="claude-haiku-5-5",
                display_name="Claude Haiku 5.5",
                model_id="claude-haiku-5-5",
                context_window=1_000_000,
                max_output_tokens=32_768,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_ANTHROPIC_HAIKU_REASONING,
            ),
        ),
        documentation_url="https://platform.claude.com/docs/en/models/overview",
        verified_on=_VERIFIED_ON,
        revision=4,
    ),
)
