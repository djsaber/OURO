"""Deepseek Search plugin entrypoint."""

from __future__ import annotations

from ouro.sdk.plugins.v5 import PluginContext

from .plugin import DeepSeekSearchPlugin


def create_plugin(context: PluginContext) -> DeepSeekSearchPlugin:
    return DeepSeekSearchPlugin(context)


__all__ = ["create_plugin"]
