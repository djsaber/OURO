"""Explicit state migrations for renamed builtin plugin identities."""

from __future__ import annotations

from ouro.core.errors import MigrationRequiredError
from ouro.plugins.models import PluginStateDocument
from ouro.plugins.store import PluginPackageStore, PluginStateFormatError

_RENAMED_BUILTINS = {
    "dev.ouro.system_information": "dev.ouro.agent_context",
}


def migrate_builtin_plugin_state(store: PluginPackageStore) -> None:
    """Move sparse builtin activation overrides to their current identities."""

    try:
        state = store.read_state()
    except PluginStateFormatError:
        # Runtime startup owns quarantine and the corresponding user-visible diagnostic.
        return
    if not any(old_id in state.builtin_activation_overrides for old_id in _RENAMED_BUILTINS):
        return

    def migrate(current: PluginStateDocument) -> PluginStateDocument:
        for old_id, new_id in _RENAMED_BUILTINS.items():
            old_override = current.builtin_activation_overrides.pop(old_id, None)
            if old_override is None:
                continue
            new_override = current.builtin_activation_overrides.get(new_id)
            if new_override is not None and new_override != old_override:
                raise MigrationRequiredError(
                    "conflicting builtin activation overrides for renamed plugin: "
                    f"{old_id} -> {new_id}"
                )
            current.builtin_activation_overrides[new_id] = old_override
        return current

    store.mutate_state(migrate)


__all__ = ["migrate_builtin_plugin_state"]
