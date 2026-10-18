from __future__ import annotations

import copy
import json

import pytest

from ouro.application.plugins import PluginService
from ouro.plugins.installer import PluginInstaller
from ouro.plugins.models import PluginConfigurationRevision, create_configuration_revision
from ouro.plugins.readiness import configuration_errors
from ouro.plugins.store import PluginPackageStore
from tests.plugins.test_plugin_system import _runtime, _write_native_plugin


def test_revision_detaches_and_freezes_nested_values_but_serializes_as_json():
    values = {"groups": [{"enabled": True}]}
    refs = {"api": "env:KEY"}
    revision = create_configuration_revision(values=values, credential_refs=refs)
    values["groups"][0]["enabled"] = False
    refs["api"] = "env:OTHER"
    with pytest.raises(TypeError):
        revision.values["groups"][0]["enabled"] = False
    with pytest.raises(TypeError):
        revision.values["groups"][0] = {}
    with pytest.raises(TypeError):
        revision.credential_refs["api"] = "env:OTHER"
    document = revision.model_dump(mode="json")
    assert document["values"] == {"groups": [{"enabled": True}]}
    assert document["credential_refs"] == {"api": "env:KEY"}
    assert PluginConfigurationRevision.model_validate_json(revision.model_dump_json()) == revision
    assert copy.deepcopy(revision) == revision
    document["values"]["groups"].clear()
    assert len(revision.values["groups"]) == 1
    with pytest.raises(ValueError, match="revision id"):
        revision.model_copy(update={"values": {"changed": True}})


@pytest.mark.parametrize("values", [{"x": float("inf")}, {1: "x"}, {"x": (1, 2)}, []])
def test_revision_rejects_non_json_input_before_hashing(values):
    with pytest.raises(ValueError):
        create_configuration_revision(values=values)


def test_install_materializes_defaults_once_and_retains_incomplete_initial_values(tmp_path):
    source = tmp_path / "package"
    _write_native_plugin(source, extra={"configuration": {"schema": "./config.json"}})
    (source / "config.json").write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "nested": {"type": "object", "default": {}, "properties": {"n": {"default": 4}}}
                },
                "required": ["user_value"],
            }
        ),
        encoding="utf-8",
    )
    store = PluginPackageStore(tmp_path / "home")
    installed = PluginInstaller(store).install(source).installed
    activation = store.read_state().plugins[installed.plugin_id].activation
    revision = activation.configuration_for(installed.package_digest)
    assert revision.values["nested"]["n"] == 4
    assert configuration_errors(
        installed, activation, store.package_path(installed.plugin_id, installed.package_digest)
    )
    assert not activation.desired_enabled
    assert PluginInstaller(store).install(source).already_installed
    assert store.read_state().plugins[installed.plugin_id].activation == activation


@pytest.mark.parametrize("kind", ["declarative", "python-inprocess", "worker"])
async def test_required_credentials_fail_before_plugin_code_and_do_not_echo_resolver_errors(
    tmp_path, kind
):
    source = tmp_path / "package"
    runtime_spec = {"kind": kind}
    if kind == "python-inprocess":
        runtime_spec["entrypoint"] = "echo_plugin:create_plugin"
    elif kind == "worker":
        runtime_spec["command"] = ["{python}", "./echo_plugin.py"]
    _write_native_plugin(
        source,
        runtime=runtime_spec,
        extra={
            "configuration": {"credentials": {"api": {"required": True}}},
            "permissions": [{"id": "credentials.use", "scopes": ["api"], "reason": "Test"}],
        },
    )
    (source / "echo_plugin.py").write_text(
        "raise RuntimeError('PLUGIN_CODE_RAN')", encoding="utf-8"
    )
    runtime, *_ = _runtime(tmp_path / "home")

    def unavailable(_reference):
        raise RuntimeError("PRIVATE_RESOLVER_PAYLOAD")

    runtime.credential_resolver = unavailable
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    await service.configure(
        service.configuration("dev.ouro.echo").candidate(
            values={}, credential_refs={"api": "env:KEY"}
        )
    )
    try:
        result = await service.enable("dev.ouro.echo")
        assert result.status == "failed"
        diagnostics = str(service.diagnostics("dev.ouro.echo"))
        assert "credential slot cannot be resolved" in diagnostics
        assert "PLUGIN_CODE_RAN" not in diagnostics
        assert "PRIVATE_RESOLVER_PAYLOAD" not in diagnostics
    finally:
        await runtime.close()


async def test_sdk_receives_recursive_read_only_snapshot(tmp_path):
    source = tmp_path / "package"
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
        extra={"configuration": {"schema": "./config.json"}},
    )
    (source / "config.json").write_text(
        json.dumps({"type": "object", "properties": {"groups": {"default": [{"enabled": True}]}}}),
        encoding="utf-8",
    )
    (source / "echo_plugin.py").write_text(
        """
from ouro.sdk.plugins.v5 import ContributionSet
class Plugin:
    def __init__(self, context): self.context = context
    async def prepare(self):
        groups = self.context.configuration['groups']
        assert isinstance(groups, tuple)
        try:
            groups[0]['enabled'] = False
        except TypeError:
            return ContributionSet()
        raise AssertionError('mutable configuration')
    async def start(self): pass
    async def stop(self, reason, deadline): pass
def create_plugin(context): return Plugin(context)
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    try:
        assert (await service.enable("dev.ouro.echo")).status == "active"
        detail = service.show("dev.ouro.echo")
        detail.configuration_values["groups"][0]["enabled"] = False
        assert service.show("dev.ouro.echo").configuration_values == {"groups": [{"enabled": True}]}
    finally:
        await runtime.close()


async def test_worker_receives_independent_json_configuration_snapshot(tmp_path):
    source = tmp_path / "package"
    _write_native_plugin(
        source,
        runtime={"kind": "worker", "command": ["{python}", "./worker.py"], "protocol": "3.0"},
        extra={"configuration": {"schema": "./config.json"}},
    )
    (source / "config.json").write_text(
        json.dumps({"type": "object", "properties": {"groups": {"default": [{"enabled": True}]}}}),
        encoding="utf-8",
    )
    (source / "worker.py").write_text(
        """import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    result = {}
    if method == 'initialize':
        groups = request['params']['configuration']['groups']
        assert isinstance(groups, list) and groups == [{'enabled': True}]
        groups[0]['enabled'] = False
        result = {'protocol_version': '3.0'}
    if 'id' in request:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
    if method == 'shutdown':
        break
""",
        encoding="utf-8",
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    before = service.configuration("dev.ouro.echo")
    try:
        assert (await service.enable("dev.ouro.echo")).status == "active"
        after = service.configuration("dev.ouro.echo")
        assert after.configuration_revision_id == before.configuration_revision_id
        assert after.values == {"groups": [{"enabled": True}]}
    finally:
        await runtime.close()
