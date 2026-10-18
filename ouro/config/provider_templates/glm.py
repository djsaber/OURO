"""GLM provider presets and native reasoning declarations."""

from __future__ import annotations

from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.config.reasoning import mapped_reasoning, toggle_reasoning
from ouro.core.reasoning import ReasoningRequest

_VERIFIED_ON = "2026-10-10"

_GLM_REASONING = toggle_reasoning(
    name="glm-thinking-toggle",
    enabled={"thinking": {"type": "enabled", "clear_thinking": False}},
    disabled={"thinking": {"type": "disabled"}},
)

_GLM_53_REASONING = mapped_reasoning(
    name="glm-5.3-thinking-effort",
    activation="always_on",
    supported_efforts=("low", "high", "max"),
    default=ReasoningRequest(mode="enabled", effort="max"),
    effort={
        "enabled": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
        **{
            effort: {
                "thinking": {"type": "enabled"},
                "reasoning_effort": effort,
            }
            for effort in ("low", "high", "max")
        },
    },
)

_GLM_53_FLASH_REASONING = mapped_reasoning(
    name="glm-5.3-flash-thinking-effort",
    activation="always_on",
    supported_efforts=("low", "high", "max"),
    default=ReasoningRequest(mode="enabled", effort="max"),
    effort={
        "enabled": {
            "thinking": {"type": "enabled", "clear_thinking": False},
            "reasoning_effort": "max",
        },
        **{
            effort: {
                "thinking": {"type": "enabled", "clear_thinking": False},
                "reasoning_effort": effort,
            }
            for effort in ("low", "high", "max")
        },
    },
)


TEMPLATES: tuple[ProviderTemplate, ...] = (
    ProviderTemplate(
        template_id="glm",
        display_name="Zhipu GLM",
        description="Zhipu's Chat Completions API with current text and multimodal models.",
        provider_type="glm",
        protocol="openai-chat-completions",
        credential_env="ZAI_API_KEY",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_path="/chat/completions",
        default_endpoint="glm-5.3",
        endpoints=(
            ProviderEndpointConfig(
                endpoint_id="glm-5.3",
                display_name="GLM-5.3",
                model_id="glm-5.3",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GLM_53_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="glm-5.3-flash",
                display_name="GLM-5.3 Flash",
                model_id="glm-5.3-flash",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GLM_53_FLASH_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="glm-5.2",
                display_name="GLM-5.2",
                model_id="glm-5.2",
                context_window=1_000_000,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GLM_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="glm-5-turbo",
                display_name="GLM-5 Turbo",
                model_id="glm-5-turbo",
                context_window=200_000,
                supports_tools=True,
                supports_image_input=False,
                supports_streaming=True,
                supports_structured_output=True,
                reasoning=_GLM_REASONING,
            ),
            ProviderEndpointConfig(
                endpoint_id="glm-5v-turbo",
                display_name="GLM-5V Turbo",
                model_id="glm-5v-turbo",
                context_window=200_000,
                supports_tools=True,
                supports_image_input=True,
                supports_streaming=True,
                supports_structured_output=False,
                reasoning=_GLM_REASONING,
            ),
        ),
        documentation_url="https://docs.bigmodel.cn/cn/guide/start/model-overview",
        verified_on=_VERIFIED_ON,
        revision=9,
    ),
)
