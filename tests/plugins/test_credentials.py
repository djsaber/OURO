from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import SecretStr

from ouro.application.plugins import PluginService
from ouro.core.redaction import SensitiveValueRedactor
from ouro.plugins.api import BoundCredentialResolver
from ouro.plugins.credentials import InlinePluginCredential, parse_credential_bindings
from ouro.plugins.mcp import PluginMCPResource
from ouro.plugins.models import PluginConfigurationRevision, create_configuration_revision
from ouro.plugins.package import validate_plugin_package
from tests.plugins.test_plugin_system import _runtime, _write_native_plugin


def test_environment_configuration_keeps_its_existing_revision_digest():
    payload = {"schema_version": 1, "values": {}, "credential_refs": {"api": "env:API_KEY"}}
    expected = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    revision = create_configuration_revision(
        credential_refs=payload["credential_refs"], schema_version=1
    )
    assert revision.revision_id == expected


def test_inline_credentials_serialize_safely_and_round_trip_exact_values():
    secret = "env:THIS_IS_THE_LITERAL_KEY"
    bindings = parse_credential_bindings({"api": {"type": "inline", "value": secret}})
    revision = create_configuration_revision(credential_refs=bindings)
    assert secret not in repr(revision)
    assert secret not in revision.model_dump_json()
    stored = revision.model_dump_json(context={"include_secrets": True})
    restored = PluginConfigurationRevision.model_validate_json(stored)
    assert restored == revision
    assert restored.credential_refs["api"].value.get_secret_value() == secret
    changed = create_configuration_revision(
        credential_refs={"api": InlinePluginCredential(value=SecretStr("different-key"))}
    )
    assert changed.revision_id != revision.revision_id


def test_inline_credentials_resolve_without_an_environment_resolver_and_are_redacted():
    secret = "env:LITERAL_KEY"
    redactor = SensitiveValueRedactor()
    resolver = BoundCredentialResolver(
        {"api": InlinePluginCredential(value=SecretStr(secret))}, None, redactor
    )
    assert resolver.resolve("api") == secret
    assert secret not in redactor.redact(f"Failed with {secret}")
    with pytest.raises(PermissionError, match="not bound"):
        resolver.resolve("other")


@pytest.mark.parametrize("binding", [{"type": "other", "value": "PRIVATE"}, {"value": ["PRIVATE"]}])
def test_invalid_credential_shapes_do_not_echo_values(binding):
    with pytest.raises(ValueError) as error:
        parse_credential_bindings({"api": binding})
    assert "PRIVATE" not in str(error.value)


def test_plugin_mcp_resolves_inline_slots_without_a_host_resolver(tmp_path):
    _write_native_plugin(
        tmp_path,
        extra={"permissions": [{"id": "credentials.use", "scopes": ["api"], "reason": "Test"}]},
    )
    manifest = validate_plugin_package(tmp_path).manifest
    resource = PluginMCPResource(
        manifest,
        tmp_path,
        credential_bindings={"api": InlinePluginCredential(value=SecretStr("env:literal-key"))},
    )
    assert resource._resolve_plugin_credential("slot:api") == "env:literal-key"
    with pytest.raises(PermissionError):
        resource._resolve_plugin_credential("slot:undeclared")


async def test_required_inline_key_activates_and_restarts_without_environment_access(tmp_path):
    source = tmp_path / "plugin"
    _write_native_plugin(
        source,
        extra={
            "configuration": {"credentials": {"api": {"required": True}}},
            "permissions": [{"id": "credentials.use", "scopes": ["api"], "reason": "Test"}],
        },
    )
    runtime, *_ = _runtime(tmp_path / "home")
    service = PluginService(runtime.store, runtime=runtime)
    service.install(source)
    await service.configure(
        service.configuration("dev.ouro.echo").candidate(
            credential_refs={"api": InlinePluginCredential(value=SecretStr("saved-api-key"))}
        )
    )
    try:
        assert (await service.enable("dev.ouro.echo")).status == "active"
        assert (await service.restart("dev.ouro.echo")).status == "active"
    finally:
        await runtime.close()
