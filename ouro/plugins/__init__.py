"""Plugin discovery and registration boundaries."""

from ouro.plugins.contributions import PluginContributions
from ouro.plugins.manifest import PluginCapability, PluginManifest
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandError,
    PluginCommandRegistry,
    PluginCommandRequest,
    PluginCommandResult,
    PluginContextProvider,
)

__all__ = [
    "PluginCommand",
    "PluginCommandError",
    "PluginCommandRequest",
    "PluginCommandRegistry",
    "PluginCommandResult",
    "PluginCapability",
    "PluginContributions",
    "PluginContextProvider",
    "PluginManifest",
]
