from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from ouro.core.plugin_contracts import PluginImplementation

# Persisted execution IDs include the canonical dependency lock, including for plugins with none.
_EMPTY_LOCK = "81059fe6210ccee4e3349c0f34c12d180f995150128a913d63b65b7937c6b152"


def build_execution_contract_hash(
    *,
    plugin_id: str,
    release_id: str,
    runtime_kind: str,
    configuration_revision_id: str,
    dependencies: Iterable[PluginImplementation],
    plugin_api_version: str,
    worker_protocol_version: str | None,
) -> str:
    """Build the stable identity used to reconstruct a generation after restart."""
    direct = [
        {"plugin_id": item.plugin_id, "execution_contract_hash": item.execution_contract_hash}
        for item in sorted(dependencies, key=lambda item: item.plugin_id)
    ]
    return _content_hash(
        {
            "plugin_id": plugin_id,
            "release_id": release_id,
            "runtime_kind": runtime_kind,
            "configuration_revision_id": configuration_revision_id,
            "dependency_lock_fingerprint": _content_hash({"dependencies": direct})
            if direct
            else _EMPTY_LOCK,
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
