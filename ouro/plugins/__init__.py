"""Plugin discovery and registration boundaries."""

from ouro.plugins.loader import DiscoveredPlugin, PluginContributions, PluginLoader
from ouro.plugins.manifest import PluginCapability, PluginManifest, PluginPermissions
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandError,
    PluginCommandRegistry,
    PluginCommandRequest,
    PluginCommandResult,
    PluginContextProvider,
)

__all__ = [
    "DiscoveredPlugin",
    "PluginCommand",
    "PluginCommandError",
    "PluginCommandRequest",
    "PluginCommandRegistry",
    "PluginCommandResult",
    "PluginCapability",
    "PluginContributions",
    "PluginContextProvider",
    "PluginLoader",
    "PluginManifest",
    "PluginPermissions",
]
