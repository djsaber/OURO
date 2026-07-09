"""Plugin discovery and registration boundaries."""

from ouro.plugins.loader import DiscoveredPlugin, PluginContributions, PluginLoader
from ouro.plugins.manifest import PluginManifest, PluginPermissions
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandRegistry,
    PluginContextProvider,
)

__all__ = [
    "DiscoveredPlugin",
    "PluginCommand",
    "PluginCommandRegistry",
    "PluginContributions",
    "PluginContextProvider",
    "PluginLoader",
    "PluginManifest",
    "PluginPermissions",
]
