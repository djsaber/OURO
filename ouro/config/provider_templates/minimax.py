"""MiniMax provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import openai_chat_completions_effort_reasoning, toggle_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-11-11"

_MINIMAX_REASONING = toggle_reasoning(
    name="minimax-adaptive-thinking",
    text_field="reasoning_content",
    enabled={"thinking": {"type": "adaptive"}},
    disabled={"thinking": {"type": "disabled"}},
)

_MINIMAX_FLASH_REASONING = openai_chat_completions_effort_reasoning(
    activation="always_on",
    supported_efforts=("low", "medium", "high", "xhigh", "max"),
    default=ReasoningRequest(mode="enabled", effort="max"),
    text_field="reasoning_content",
)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="minimax",
        display_name="MiniMax",
        description=(
            "MiniMax's China Chat Completions endpoint with multimodal M3 and M3.1 Flash."
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
                endpoint_id="MiniMax-M3.1-Flash",
                display_name="MiniMax M3.1 Flash",
                model_id="MiniMax-M3.1-Flash",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_MINIMAX_FLASH_REASONING,
                request_options={"reasoning_split": True},
            ),
        ),
        documentation_url="https://platform.minimax.cn/docs/api-reference/text-openai-api",
        verified_on=_VERIFIED_ON,
        revision=6,
    ),
)
