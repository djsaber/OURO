from __future__ import annotations

import hashlib
import json

import pytest

from ouro.plugins.execution_contracts import build_execution_contract_hash


def _contract_hash(**changes: object) -> str:
    values: dict[str, object] = {
        "plugin_id": "dev.ouro.example",
        "version": "1.0.0",
        "plugin_class": "user",
        "release_id": "a" * 64,
        "runtime_kind": "python-worker",
        "configuration_revision_id": "b" * 64,
        "dependency_lock_fingerprint": "c" * 64,
        "plugin_api_version": "1",
        "worker_protocol_version": "1",
        "contribution_contract_hashes": {
            "dev.ouro.example/tools/run": "d" * 64,
            "dev.ouro.example/hooks/after": "e" * 64,
        },
    }
    values.update(changes)
    return build_execution_contract_hash(**values)  # type: ignore[arg-type]


def test_execution_contract_hash_is_canonical() -> None:
    first = _contract_hash()
    second = _contract_hash(
        contribution_contract_hashes={
            "dev.ouro.example/hooks/after": "e" * 64,
            "dev.ouro.example/tools/run": "d" * 64,
        }
    )

    assert first == second
    assert len(first) == 64


def test_user_execution_contract_remains_compatible_with_package_digest_schema() -> None:
    payload = {
        "schema_version": 1,
        "plugin_id": "dev.ouro.example",
        "version": "1.0.0",
        "package_digest": "a" * 64,
        "runtime_kind": "python-worker",
        "configuration_revision_id": "b" * 64,
        "dependency_lock_fingerprint": "c" * 64,
        "plugin_api_version": "1",
        "worker_protocol_version": "1",
        "contribution_contract_hashes": {
            "dev.ouro.example/tools/run": "d" * 64,
            "dev.ouro.example/hooks/after": "e" * 64,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    assert _contract_hash() == hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("release_id", "f" * 64),
        ("plugin_class", "builtin"),
        ("configuration_revision_id", "f" * 64),
        ("dependency_lock_fingerprint", "f" * 64),
        ("plugin_api_version", "2"),
        ("worker_protocol_version", "2"),
        (
            "contribution_contract_hashes",
            {"dev.ouro.example/tools/run": "f" * 64},
        ),
    ],
)
def test_execution_contract_hash_changes_with_runtime_facts(field: str, value: object) -> None:
    assert _contract_hash(**{field: value}) != _contract_hash()
