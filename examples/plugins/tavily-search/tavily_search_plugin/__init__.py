"""Tavily Search plugin entrypoint."""

from __future__ import annotations

from ouro.sdk.plugins.v6 import PluginContext

from .plugin import TavilySearchPlugin


def create_plugin(context: PluginContext) -> TavilySearchPlugin:
    return TavilySearchPlugin(context)


__all__ = ["create_plugin"]
