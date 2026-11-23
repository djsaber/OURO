from __future__ import annotations

from dataclasses import replace

from ouro.core.extensions import PluginSource
from ouro.core.session_controller_registry import SessionController
from ouro.plugins.models import PluginClass


def bind_plugin_session_controller(
    controller: SessionController,
    *,
    plugin_id: str,
    source: PluginSource,
    plugin_class: PluginClass,
    release_id: str,
    package_digest: str | None,
    generation_id: str,
    execution_contract_hash: str | None = None,
    maximum_timeout_seconds: float,
) -> SessionController:
    return replace(
        controller,
        plugin_id=plugin_id,
        source=source,
        plugin_class=plugin_class,
        release_id=release_id,
        package_digest=package_digest,
        generation_id=generation_id,
        execution_contract_hash=execution_contract_hash,
        timeout_seconds=min(controller.timeout_seconds, maximum_timeout_seconds),
    )
