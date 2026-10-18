from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import ouro.plugins.package as package_module
from ouro.application.plugins import PluginService
from ouro.plugins.configuration import credential_binding_issues
from ouro.plugins.installer import PluginInstaller
from ouro.plugins.manifest import PluginManifest
from ouro.plugins.package import PluginPackageError, validate_plugin_package
from ouro.plugins.store import PluginPackageStore
from tests.plugins.test_plugin_system import _write_native_plugin


def test_credential_only_definition_validates_metadata_and_binding_requirements(tmp_path):
    source = tmp_path / "package"
    _write_native_plugin(
        source,
        extra={
            "configuration": {"version": 2, "credentials": {"api": {"required": True}}},
            "permissions": [
                {"id": "credentials.use", "scopes": ["api", "optional"], "reason": "Test"}
            ],
        },
    )
    manifest = validate_plugin_package(source).manifest
    assert manifest.configuration.schema_path is None
    assert credential_binding_issues(manifest, {})[0].code == "required_credential"
    assert not credential_binding_issues(manifest, {"api": "env:PLUGIN_KEY"})
    assert (
        credential_binding_issues(manifest, {"api": "PRIVATE_KEY"})[0].code
        == "credential_reference"
    )
    assert "PRIVATE_KEY" not in str(credential_binding_issues(manifest, {"api": "PRIVATE_KEY"}))
    assert credential_binding_issues(manifest, {"api": "env:1INVALID"})
    assert credential_binding_issues(manifest, {"api": "env:KEY", "other": "env:OTHER"})
    raw = manifest.model_dump(by_alias=True)
    raw["configuration"]["credentials"]["undeclared"] = {}
    with pytest.raises(ValidationError, match="credential"):
        PluginManifest.model_validate(raw)


async def test_required_credentials_install_disabled_and_configure_without_value_schema(tmp_path):
    source = tmp_path / "package"
    _write_native_plugin(
        source,
        extra={
            "configuration": {"credentials": {"api": {"required": True}}},
            "permissions": [{"id": "credentials.use", "scopes": ["api"], "reason": "Test"}],
        },
    )
    store = PluginPackageStore(tmp_path / "home")
    PluginInstaller(store).install(source)
    service = PluginService(store)
    assert service.show("dev.ouro.echo").status == "disabled"
    with pytest.raises(PluginPackageError, match="required credential"):
        await service.configure(service.configuration("dev.ouro.echo").candidate(values={}))
    await service.configure(
        service.configuration("dev.ouro.echo").candidate(
            values={}, credential_refs={"api": "env:KEY"}
        )
    )
    assert service.show("dev.ouro.echo").credential_refs == {"api": "env:KEY"}


def test_package_validation_checks_schema_without_importing_plugin_code(tmp_path):
    source = tmp_path / "package"
    _write_native_plugin(
        source,
        runtime={"kind": "python-inprocess", "entrypoint": "echo_plugin:create_plugin"},
        extra={"configuration": {"schema": "./config.json"}},
    )
    (source / "echo_plugin.py").write_text(
        "raise AssertionError('must not import')", encoding="utf-8"
    )
    schema = {"type": "object", "required": ["user_value"]}
    (source / "config.json").write_text(json.dumps(schema), encoding="utf-8")
    validate_plugin_package(source)
    schema["properties"] = {"x": {"$ref": "https://example.test/private"}}
    (source / "config.json").write_text(json.dumps(schema), encoding="utf-8")
    with pytest.raises(PluginPackageError, match="configuration schema"):
        validate_plugin_package(source)


def test_plugin_using_configuration_contract_can_require_its_api_version(tmp_path, monkeypatch):
    _write_native_plugin(
        tmp_path / "package",
        extra={"engines": {"ouro": ">=0.3.0,<0.4.0", "plugin_api": "^5.0.0"}},
    )
    validate_plugin_package(tmp_path / "package")
    monkeypatch.setattr(package_module, "PLUGIN_API_VERSION", "3.0.0")
    with pytest.raises(PluginPackageError, match="API is incompatible"):
        validate_plugin_package(tmp_path / "package")
