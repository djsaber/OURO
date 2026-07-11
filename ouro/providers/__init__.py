"""Concrete provider adapters and their construction factory."""

from ouro.providers.factory import build_runtime_adapter
from ouro.providers.faux import FauxProvider
from ouro.providers.openai_compatible import OpenAICompatibleProvider

__all__ = [
    "FauxProvider",
    "OpenAICompatibleProvider",
    "build_runtime_adapter",
]
