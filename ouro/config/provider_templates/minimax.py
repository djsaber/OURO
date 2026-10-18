"""MiniMax provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import mapped_reasoning, toggle_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_MINIMAX_REASONING = toggle_reasoning(
    name="minimax-adaptive-thinking",
    enabled={"thinking": {"type": "adaptive"}},
    disabled={"thinking": {"type": "disabled"}},
)

_MINIMAX_M2_REASONING = mapped_reasoning(
    name="minimax-always-on-thinking",
    activation="always_on",
    default=ReasoningRequest(mode="enabled"),
    output="text",
    continuation="text",
    effort={"enabled": {}},
)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="minimax",
        display_name="MiniMax",
        description=(
            "MiniMax's official China Chat Completions endpoint with multimodal M3 and "
            "text-only M2.7 models."
        ),
        provider_type="minimax",
        protocol="openai-chat-completions",
        credential_env="MINIMAX_API_KEY",
        base_url="https://api.minimax.cn/v1",
        api_path="/chat/completions",
        default_endpoint="MiniMax-M3",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="MiniMax-M3",
                display_name="MiniMax M3",
                model_id="MiniMax-M3",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_MINIMAX_REASONING,
                request_options={"reasoning_split": True},
            ),
            ProviderEndpointConfig(
                endpoint_id="MiniMax-M2.7",
                display_name="MiniMax M2.7",
                model_id="MiniMax-M2.7",
                context_window=204_800,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_MINIMAX_M2_REASONING,
                request_options={"reasoning_split": True},
            ),
            ProviderEndpointConfig(
                endpoint_id="MiniMax-M2.7-highspeed",
                display_name="MiniMax M2.7 Highspeed",
                model_id="MiniMax-M2.7-highspeed",
                context_window=204_800,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_MINIMAX_M2_REASONING,
                request_options={"reasoning_split": True},
            ),
        ),
        documentation_url="https://platform.minimax.cn/docs/api-reference/text-openai-api",
        verified_on=_VERIFIED_ON,
        revision=4,
    ),
)
