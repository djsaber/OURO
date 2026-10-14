"""Built-in provider presets, explicitly ordered and queried by template ID."""

from __future__ import annotations

from ouro.config.provider_templates.anthropic import TEMPLATES as _ANTHROPIC
from ouro.config.provider_templates.cohere import TEMPLATES as _COHERE
from ouro.config.provider_templates.deepseek import TEMPLATES as _DEEPSEEK
from ouro.config.provider_templates.doubao import TEMPLATES as _DOUBAO
from ouro.config.provider_templates.gemini import TEMPLATES as _GEMINI
from ouro.config.provider_templates.glm import TEMPLATES as _GLM
from ouro.config.provider_templates.grok import TEMPLATES as _GROK
from ouro.config.provider_templates.kimi import TEMPLATES as _KIMI
from ouro.config.provider_templates.minimax import TEMPLATES as _MINIMAX
from ouro.config.provider_templates.mistral import TEMPLATES as _MISTRAL
from ouro.config.provider_templates.openai import TEMPLATES as _OPENAI
from ouro.config.provider_templates.opencode_go import TEMPLATES as _OPENCODE_GO
from ouro.config.provider_templates.qwen import TEMPLATES as _QWEN
from ouro.config.provider_templates.schema import ProviderTemplate
from ouro.core.errors import ConfigError

__all__ = ["ProviderTemplate", "get_provider_template", "provider_templates"]

_PROVIDER_TEMPLATES: tuple[ProviderTemplate, ...] = (
    *_OPENAI,
    *_ANTHROPIC,
    *_GEMINI,
    *_DEEPSEEK,
    *_QWEN,
    *_KIMI,
    *_GLM,
    *_GROK,
    *_MISTRAL,
    *_MINIMAX,
    *_DOUBAO,
    *_COHERE,
    *_OPENCODE_GO,
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
