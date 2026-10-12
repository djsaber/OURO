import pytest

from ouro.core.plugin_contracts import PluginImplementation
from ouro.core.run_drivers import RunDriverBinding
from ouro.plugins.dependencies import BuiltinPluginContract
from ouro.plugins.driver_contracts import resolve_driver_contract
from ouro.plugins.models import BuiltinPluginActivationOverride, PluginStateDocument


@pytest.mark.parametrize("availability", ["matching", "missing", "replaced", "disabled"])
def test_builtin_driver_requires_the_original_available_application_contract(availability):
    plugin_id = "dev.ouro.driver"
    binding = RunDriverBinding(
        driver_id=f"{plugin_id}/run_drivers/main",
        contract_hash="entry",
        implementation=PluginImplementation(
            plugin_id=plugin_id,
            plugin_class="builtin",
            release_id="original",
            configuration_revision_id="configuration",
            execution_contract_hash="execution",
        ),
    )
    state = PluginStateDocument()
    builtins = {
        plugin_id: BuiltinPluginContract(
            plugin_id, "1.0.0", "replacement" if availability == "replaced" else "original"
        )
    }
    if availability == "missing":
        builtins.clear()
    if availability == "disabled":
        state.builtin_activation_overrides[plugin_id] = BuiltinPluginActivationOverride(
            desired_enabled=False
        )
    if availability == "matching":
        selected, graph = resolve_driver_contract(binding, state, builtins)
        assert selected == state
        assert graph.activation_order == (plugin_id,)
    else:
        with pytest.raises(ValueError, match="disabled|contract is unavailable"):
            resolve_driver_contract(binding, state, builtins)
