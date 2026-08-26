from ouro.builtin_plugins.agent_context import AGENT_CONTEXT_PLUGIN
from ouro.builtin_plugins.goal import GOAL_PLUGIN
from ouro.builtin_plugins.plan import PLAN_PLUGIN
from ouro.builtin_plugins.toolkit import TOOLKIT_PLUGIN
from ouro.plugins.builtin import BuiltinPluginRegistry

LOCAL_BUILTIN_PLUGINS = BuiltinPluginRegistry(
    (
        GOAL_PLUGIN,
        PLAN_PLUGIN,
        AGENT_CONTEXT_PLUGIN,
        TOOLKIT_PLUGIN,
    )
)

__all__ = ["LOCAL_BUILTIN_PLUGINS"]
