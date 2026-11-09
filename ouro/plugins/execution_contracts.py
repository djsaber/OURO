from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from ouro.core.plugin_contracts import PluginImplementation


def build_dependency_lock_fingerprint(
    dependencies: Iterable[PluginImplementation],
) -> str:
    """Bind direct implementations; their execution contracts bind the transitive closure."""
    return _content_hash(
        {
            "dependencies": [
                {
                    "plugin_id": dependency.plugin_id,
                    "execution_contract_hash": dependency.execution_contract_hash,
                }
                for dependency in sorted(dependencies, key=lambda item: item.plugin_id)
            ]
        }
    )


def build_execution_contract_hash(
    *,
    plugin_id: str,
    release_id: str,
    runtime_kind: str,
    configuration_revision_id: str,
    dependency_lock_fingerprint: str,
    plugin_api_version: str,
    worker_protocol_version: str | None,
) -> str:
    """Build the stable identity used to reconstruct a generation after restart."""
    return _content_hash(
        {
            "plugin_id": plugin_id,
            "release_id": release_id,
            "runtime_kind": runtime_kind,
            "configuration_revision_id": configuration_revision_id,
            "dependency_lock_fingerprint": dependency_lock_fingerprint,
            "plugin_api_version": plugin_api_version,
            "worker_protocol_version": worker_protocol_version,
        }
    )


def _content_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "build_dependency_lock_fingerprint",
    "build_execution_contract_hash",
]
