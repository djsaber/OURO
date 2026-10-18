"""Qwen provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import mapped_reasoning, toggle_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_QWEN_REASONING = toggle_reasoning(
    name="qwen-thinking-toggle",
    enabled={"enable_thinking": True, "preserve_thinking": True},
    disabled={"enable_thinking": False, "preserve_thinking": False},
)

_QWEN_FLASH_REASONING = mapped_reasoning(
    name="qwen-flash-thinking-effort",
    activation="toggle",
    supported_efforts=("low", "medium", "xhigh"),
    default=ReasoningRequest(mode="enabled", effort="xhigh"),
    output="text",
    continuation="text",
    effort={
        "disabled": {"enable_thinking": False, "preserve_thinking": False},
        "enabled": {
            "enable_thinking": True,
            "preserve_thinking": True,
            "reasoning_effort": "xhigh",
        },
        **{
            effort: {
                "enable_thinking": True,
                "preserve_thinking": True,
                "reasoning_effort": effort,
            }
            for effort in ("low", "medium", "xhigh")
        },
    },
)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="qwen",
        display_name="Alibaba Cloud Qwen",
        description=(
            "DashScope's OpenAI Chat Completions-compatible China endpoint with the Qwen3.8-Max "
            "flagship, Qwen3.8-Flash, and Qwen3.7-Plus multimodal models."
        ),
        provider_type="qwen",
        protocol="openai-chat-completions",
        credential_env="DASHSCOPE_API_KEY",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_path="/chat/completions",
        default_endpoint="qwen3.8-max",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="qwen3.8-max",
                display_name="Qwen3.8 Max",
                model_id="qwen3.8-max",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_QWEN_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="qwen3.8-flash",
                display_name="Qwen3.8 Flash",
                model_id="qwen3.8-flash",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_QWEN_FLASH_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="qwen3.7-plus",
                display_name="Qwen3.7 Plus",
                model_id="qwen3.7-plus",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=True,
                supports_video_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_QWEN_REASONING,
            ),
        ),
        documentation_url="https://help.aliyun.com/zh/model-studio/text-generation-model",
        verified_on=_VERIFIED_ON,
        revision=10,
    ),
)
