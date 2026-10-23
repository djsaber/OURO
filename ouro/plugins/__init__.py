"""Plugin discovery and registration boundaries."""

from ouro.plugins.contributions.models import PluginContributions
from ouro.plugins.manifest import PluginCapability, PluginManifest
from ouro.plugins.registry import (
    PluginCommand,
    PluginCommandError,
    PluginCommandRegistry,
    PluginCommandRequest,
    PluginCommandResult,
)

__all__ = [
    "PluginCommand",
    "PluginCommandError",
    "PluginCommandRequest",
    "PluginCommandRegistry",
    "PluginCommandResult",
    "PluginCapability",
    "PluginContributions",
    "PluginManifest",
]
