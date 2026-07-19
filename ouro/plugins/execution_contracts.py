from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from ouro.plugins.dependencies import PluginLockGraph
from ouro.plugins.models import PluginGenerationRecord, PluginStateDocument


def build_dependency_lock_fingerprint(
    graph: PluginLockGraph,
    state: PluginStateDocument,
    generations: Mapping[str, PluginGenerationRecord],
    *,
    owner_plugin_id: str,
) -> str:
    """Hash the resolved transitive dependencies of one candidate generation."""

    locked: list[dict[str, object]] = []
    for node in sorted(graph.nodes, key=lambda item: item.plugin_id):
        if node.plugin_id == owner_plugin_id:
            continue
        item: dict[str, object] = {
            "plugin_id": node.plugin_id,
            "version": node.version,
            "plugin_class": node.plugin_class,
            "dependencies": list(node.dependencies),
        }
        if node.plugin_class == "builtin":
            if node.contract_hash is None:
                raise ValueError(f"builtin dependency contract is missing: {node.plugin_id}")
            item["builtin_contract_hash"] = node.contract_hash
        else:
            record = state.plugins[node.plugin_id]
            selected = record.selected()
            if selected is None or selected.package_digest != node.package_digest:
                raise ValueError(f"user dependency selection is inconsistent: {node.plugin_id}")
            generation = generations.get(node.plugin_id)
            if generation is None or generation.execution_contract_hash is None:
                raise ValueError(f"user dependency execution contract is missing: {node.plugin_id}")
            revision_id = record.activation.active_configuration_revision_id
            if revision_id is None or revision_id != generation.configuration_revision_id:
                raise ValueError(
                    f"user dependency configuration contract is inconsistent: {node.plugin_id}"
                )
            if generation.package_digest != selected.package_digest:
                raise ValueError(f"user dependency generation is stale: {node.plugin_id}")
            item.update(
                {
                    "package_digest": selected.package_digest,
                    "configuration_revision_id": revision_id,
                    "execution_contract_hash": generation.execution_contract_hash,
                }
            )
        locked.append(item)
    return _content_hash({"dependencies": locked})


def build_execution_contract_hash(
    *,
    plugin_id: str,
    version: str,
    plugin_class: str,
    release_id: str,
    runtime_kind: str,
    configuration_revision_id: str,
    dependency_lock_fingerprint: str,
    plugin_api_version: str,
    worker_protocol_version: str | None,
    contribution_contract_hashes: Mapping[str, str],
) -> str:
    """Build the stable identity used to reconstruct a generation after restart."""

    if plugin_class == "user":
        release_contract = {"package_digest": release_id}
    elif plugin_class == "builtin":
        release_contract = {
            "plugin_class": "builtin",
            "builtin_contract_hash": release_id,
        }
    else:
        raise ValueError(f"unsupported plugin class: {plugin_class}")
    return _content_hash(
        {
            "schema_version": 1,
            "plugin_id": plugin_id,
            "version": version,
            **release_contract,
            "runtime_kind": runtime_kind,
            "configuration_revision_id": configuration_revision_id,
            "dependency_lock_fingerprint": dependency_lock_fingerprint,
            "plugin_api_version": plugin_api_version,
            "worker_protocol_version": worker_protocol_version,
            "contribution_contract_hashes": dict(contribution_contract_hashes),
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
