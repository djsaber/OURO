from ouro.builtin_plugins.agent_context import AGENT_CONTEXT_PLUGIN
from ouro.builtin_plugins.goal_mode import GOAL_MODE_PLUGIN
from ouro.builtin_plugins.plan_mode import PLAN_MODE_PLUGIN
from ouro.builtin_plugins.toolkit import TOOLKIT_PLUGIN
from ouro.plugins.builtin import BuiltinPluginRegistry

LOCAL_BUILTIN_PLUGINS = BuiltinPluginRegistry(
    (
        GOAL_MODE_PLUGIN,
        PLAN_MODE_PLUGIN,
        AGENT_CONTEXT_PLUGIN,
        TOOLKIT_PLUGIN,
    )
)

__all__ = ["LOCAL_BUILTIN_PLUGINS"]
