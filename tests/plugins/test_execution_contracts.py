from __future__ import annotations

import pytest

from ouro.core.plugin_contracts import PluginImplementation
from ouro.plugins.execution_contracts import build_execution_contract_hash


def _contract_hash(**changes: object) -> str:
    values: dict[str, object] = {
        "plugin_id": "dev.ouro.example",
        "release_id": "a" * 64,
        "runtime_kind": "worker",
        "configuration_revision_id": "b" * 64,
        "dependencies": [],
        "plugin_api_version": "1",
        "worker_protocol_version": "1",
    }
    values.update(changes)
    return build_execution_contract_hash(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("release_id", "f" * 64),
        ("plugin_id", "dev.ouro.other"),
        ("runtime_kind", "python-inprocess"),
        ("configuration_revision_id", "f" * 64),
        ("plugin_api_version", "2"),
        ("worker_protocol_version", "2"),
    ],
)
def test_execution_contract_hash_changes_with_runtime_facts(field: str, value: object) -> None:
    assert _contract_hash(**{field: value}) != _contract_hash()


def test_dependency_fingerprint_ignores_order_and_source_class() -> None:
    first = PluginImplementation(
        plugin_id="dev.ouro.first",
        plugin_class="builtin",
        release_id="release-1",
        configuration_revision_id="config-1",
        execution_contract_hash=_contract_hash(plugin_id="dev.ouro.first"),
    )
    second = first.model_copy(
        update={
            "plugin_id": "dev.ouro.second",
            "execution_contract_hash": _contract_hash(plugin_id="dev.ouro.second"),
        }
    )
    assert _contract_hash(dependencies=[first, second]) == _contract_hash(
        dependencies=[
            second.model_copy(update={"plugin_class": "user"}),
            first.model_copy(update={"plugin_class": "user"}),
        ]
    )


@pytest.mark.parametrize("plugin_class", ["builtin", "user"])
def test_dependency_fingerprint_tracks_transitive_execution_contract(plugin_class) -> None:
    dependency = PluginImplementation(
        plugin_id="dev.ouro.dependency",
        plugin_class=plugin_class,
        release_id="release-1",
        configuration_revision_id="config-1",
        execution_contract_hash=_contract_hash(),
    )
    changed = dependency.model_copy(
        update={"execution_contract_hash": _contract_hash(release_id="new-release")}
    )
    assert _contract_hash(dependencies=[dependency]) != _contract_hash(dependencies=[changed])


def test_historical_execution_ids_survive_restart_with_and_without_dependencies() -> None:
    assert _contract_hash() == "95e91cd8b3af42e5a411e944dee1c00dbab32450e676eb701d49bff78a833503"
    dependency = PluginImplementation(
        plugin_id="dev.ouro.café",
        plugin_class="builtin",
        release_id="example",
        configuration_revision_id="configuration",
        execution_contract_hash="d" * 64,
    )
    assert _contract_hash(dependencies=[dependency]) == (
        "828d34aea9115cfd3155821139ecbeef1df06b3a6c2d6b930c7e85973f1cc2f4"
    )
