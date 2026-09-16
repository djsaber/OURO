from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from ouro.application.configuration import ConfigurationFacade
from ouro.application.configuration_views import provider_view
from ouro.auth.contracts import CredentialRecord
from ouro.config import provider_models
from ouro.config.credentials import CredentialRef, CredentialResolver
from ouro.config.provider_codec import provider_config_from_mapping, provider_config_to_public_dict
from ouro.config.provider_models import (
    ProviderConnectionSpec,
    ProviderRuntimeOptions,
    ProviderTypeSpec,
    resolve_provider_connection,
)
from ouro.config.provider_template_store import SCHEMA_VERSION, ProviderTemplateStore
from ouro.config.provider_templates import ProviderTemplate, get_provider_template
from ouro.config.providers import ProviderConfigManager
from ouro.core.base import new_id
from ouro.core.errors import ConfigError, CredentialError
from ouro.providers.factory import build_runtime_adapter


@pytest.fixture
def fixture_product(monkeypatch):
    """Only test declarations admit managed pairs; production remains static."""
    spec = ProviderTypeSpec(
        "openai-compatible",
        ProviderConnectionSpec("openai-chat-completions"),
        (
            ProviderConnectionSpec("openai-chat-completions", "browser-fixture", "service-one"),
            ProviderConnectionSpec("anthropic-messages", "device-fixture", "service-two"),
        ),
    )
    monkeypatch.setitem(provider_models._PROVIDER_TYPE_SPECS, spec.provider_type, spec)
    return spec


def managed_provider(tmp_path):
    manager = ProviderConfigManager(tmp_path)
    provider = manager.create_provider(
        "subscription",
        provider_type="openai-compatible",
        auth_driver="browser-fixture",
        base_url="https://fixture.test/v1",
    )
    manager.upsert_endpoint("subscription", "chat", model_id="fixture", make_default=True)
    manager.set_default_provider("subscription")
    return manager, provider


def test_managed_reference_round_trip_is_not_a_path_or_resolvable_secret():
    credential_id = new_id("cred")
    ref = CredentialRef(type="managed", name=credential_id)
    assert CredentialRef.model_validate_json(ref.model_dump_json()) == ref
    # The storage and configuration contracts accept the same generated identity.
    CredentialRecord(
        credential_id=credential_id, driver_id="fixture", revision=1, state="logged_out"
    )
    assert ref.display() == f"managed:{credential_id}"
    with pytest.raises(CredentialError, match="authentication service"):
        CredentialResolver().resolve(ref)


@pytest.mark.parametrize(
    "name", [None, "", "../secret", "C:/secret", "cred_short", "cred_" + "A" * 32]
)
def test_managed_reference_rejects_invalid_identity(name):
    with pytest.raises(ValidationError):
        CredentialRef(type="managed", name=name)


@pytest.mark.parametrize("extra", [{"value": "fixture-secret"}, {"token": "fixture-secret"}])
def test_managed_reference_rejects_secret_or_unknown_fields_without_echo(extra):
    with pytest.raises(ValidationError) as failure:
        CredentialRef.model_validate({"type": "managed", "name": new_id("cred"), **extra})
    assert "fixture-secret" not in str(failure.value)


def test_product_pairs_do_not_admit_their_cartesian_product(fixture_product):
    first = resolve_provider_connection("openai-compatible", None, "browser-fixture")
    second = resolve_provider_connection(
        "openai-compatible", "anthropic-messages", "device-fixture"
    )
    assert first.service_policy == "service-one"
    assert second.service_policy == "service-two"
    for protocol, driver in [
        ("anthropic-messages", "browser-fixture"),
        ("openai-chat-completions", "device-fixture"),
        ("anthropic-messages", "static"),
    ]:
        with pytest.raises(ValueError, match="combination"):
            resolve_provider_connection("openai-compatible", protocol, driver)


def test_production_profiles_do_not_claim_managed_support():
    for provider_type in provider_models.SUPPORTED_PROVIDER_TYPES:
        with pytest.raises(ValueError, match="combination"):
            resolve_provider_connection(provider_type, auth_driver="browser-fixture")


def test_product_declaration_rejects_ambiguous_pairs_and_missing_service_rules():
    connection = ProviderConnectionSpec("openai-chat-completions")
    with pytest.raises(ValueError, match="unique"):
        ProviderTypeSpec("openai-compatible", connection, (connection,))
    with pytest.raises(ValueError, match="service policy"):
        ProviderTypeSpec(
            "openai-compatible", ProviderConnectionSpec("openai-chat-completions", "fixture")
        )


def test_product_default_selects_protocol_and_auth_together(tmp_path, monkeypatch):
    monkeypatch.setitem(
        provider_models._PROVIDER_TYPE_SPECS,
        "openai-compatible",
        ProviderTypeSpec(
            "openai-compatible",
            ProviderConnectionSpec("anthropic-messages", "device-fixture", "service-two"),
        ),
    )
    provider = ProviderConfigManager(tmp_path).create_provider(
        "subscription", provider_type="openai-compatible"
    )
    assert provider.protocol == "anthropic-messages"
    assert provider.auth_driver == "device-fixture"
    assert provider.credential_ref.type == "managed"


def test_create_reload_runtime_and_public_view_need_no_credential_file(tmp_path, fixture_product):
    manager, created = managed_provider(tmp_path)
    assert created.credential_ref.type == "managed"
    reloaded = ProviderConfigManager(tmp_path).get_provider("subscription")
    assert reloaded.auth_driver == "browser-fixture"
    assert reloaded.credential_ref == created.credential_ref
    options = manager.resolve_runtime_options(ProviderRuntimeOptions())
    assert options.auth_driver == "browser-fixture"
    assert options.service_policy == "service-one"
    assert options.credential_ref == created.credential_ref
    view = provider_view(reloaded, is_default=True)
    assert view.auth_driver == "browser-fixture"
    assert view.credential.state == "managed"
    assert view.credential.configured is True
    assert view.credential.available is None
    assert provider_config_to_public_dict(reloaded)["auth_driver"] == "browser-fixture"
    raw = json.loads(manager.config_path.read_text(encoding="utf-8"))
    item = raw["providers"]["items"]["subscription"]
    assert item["credential_ref"] == {"type": "managed", "name": created.credential_ref.name}
    assert "service_policy" not in item
    assert not (tmp_path / "credentials").exists()


def test_update_preserves_binding_replace_generates_new_identity(tmp_path, fixture_product):
    manager, original = managed_provider(tmp_path)
    updated = manager.update_provider("subscription", timeout_seconds=42)
    assert updated.auth_driver == original.auth_driver
    assert updated.credential_ref == original.credential_ref
    replacement = manager.replace_provider(
        "subscription", provider_type="openai-compatible", auth_driver="browser-fixture"
    )
    assert replacement.credential_ref != original.credential_ref
    assert replacement.endpoints == ()
    manager.remove_provider("subscription")
    assert not (tmp_path / "credentials").exists()


@pytest.mark.parametrize(
    "overrides", [{"api_key": "fixture-secret"}, {"api_key_env": "FIXTURE_KEY"}]
)
def test_managed_mutations_reject_api_key_without_writing(tmp_path, fixture_product, overrides):
    manager, original = managed_provider(tmp_path)
    before = manager.config_path.read_bytes()
    for operation in (manager.update_provider, manager.create_provider, manager.replace_provider):
        kwargs = overrides.copy()
        target = "subscription"
        if operation != manager.update_provider:
            kwargs.update(provider_type="openai-compatible", auth_driver="browser-fixture")
        if operation == manager.create_provider:
            target = "another"
        with pytest.raises(ConfigError, match="API Key") as failure:
            operation(target, **kwargs)
        assert "fixture-secret" not in str(failure.value)
        assert manager.config_path.read_bytes() == before
    assert manager.get_provider("subscription").credential_ref == original.credential_ref


@pytest.mark.parametrize(
    "override",
    [
        ProviderRuntimeOptions(api_key="fixture-secret"),
        ProviderRuntimeOptions(api_key_env="FIXTURE_KEY"),
        ProviderRuntimeOptions(auth_driver="static"),
        ProviderRuntimeOptions(credential_ref=CredentialRef(type="managed", name=new_id("cred"))),
        ProviderRuntimeOptions(service_policy="untrusted-policy"),
    ],
)
def test_runtime_cannot_replace_managed_authentication(tmp_path, fixture_product, override):
    manager, _ = managed_provider(tmp_path)
    with pytest.raises(ConfigError) as failure:
        manager.resolve_runtime_options(override)
    assert "fixture-secret" not in str(failure.value)


def test_static_defaults_and_api_key_overrides_are_preserved(tmp_path):
    manager = ProviderConfigManager(tmp_path)
    manager.create_provider(
        "api", provider_type="openai-compatible", base_url="https://fixture.test"
    )
    manager.upsert_endpoint("api", "chat", model_id="fixture", make_default=True)
    options = manager.resolve_runtime_options(
        ProviderRuntimeOptions(provider_id="api", api_key="fixture-secret")
    )
    assert options.auth_driver == "static"
    assert options.api_key == "fixture-secret"
    raw = json.loads(manager.config_path.read_text(encoding="utf-8"))
    assert "auth_driver" not in raw["providers"]["items"]["api"]


@pytest.mark.parametrize(
    "driver,ref",
    [
        ("static", {"type": "managed", "name": new_id("cred")}),
        ("browser-fixture", {"type": "inline", "value": "fixture-secret"}),
        ("browser-fixture", {"type": "none"}),
    ],
)
def test_config_codec_rejects_mismatched_driver_and_reference(fixture_product, driver, ref):
    with pytest.raises(ConfigError) as failure:
        provider_config_from_mapping(
            "fixture",
            {
                "provider_type": "openai-compatible",
                "protocol": "openai-chat-completions",
                "auth_driver": driver,
                "credential_ref": ref,
            },
        )
    assert "fixture-secret" not in str(failure.value)


def test_service_policy_cannot_be_user_configured(fixture_product):
    with pytest.raises(ConfigError, match="unsupported provider config fields"):
        provider_config_from_mapping(
            "fixture",
            {
                "provider_type": "openai-compatible",
                "protocol": "openai-chat-completions",
                "service_policy": "untrusted-policy",
            },
        )


def test_factory_rejects_unwired_managed_auth_without_resolving_secrets(tmp_path, fixture_product):
    manager, _ = managed_provider(tmp_path)

    class MustNotResolve(CredentialResolver):
        def resolve(self, ref):
            pytest.fail("factory must not resolve managed material as an API Key")

    with pytest.raises(ConfigError, match="not connected"):
        build_runtime_adapter(
            manager.resolve_runtime_options(ProviderRuntimeOptions()), credentials=MustNotResolve()
        )


def test_ad_hoc_managed_auth_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="configured provider"):
        ProviderConfigManager(tmp_path).resolve_runtime_options(
            ProviderRuntimeOptions(
                protocol="openai-chat-completions",
                auth_driver="fixture",
                credential_ref=CredentialRef(type="managed", name=new_id("cred")),
            )
        )


def test_facade_creates_and_replaces_managed_config_without_resolving_credentials(
    tmp_path, fixture_product
):
    facade = ConfigurationFacade(tmp_path)
    first = facade.add_provider(
        "subscription", provider_type="openai-compatible", auth_driver="browser-fixture"
    )
    assert first.providers[0].auth_driver == "browser-fixture"
    previous = facade.provider("subscription").credential.reference
    facade.update_provider_settings("subscription", timeout=42)
    assert facade.provider("subscription").credential.reference == previous
    facade.replace_provider(
        "subscription", provider_type="openai-compatible", auth_driver="browser-fixture"
    )
    assert facade.provider("subscription").credential.reference != previous


def subscription_template(**overrides) -> ProviderTemplate:
    return ProviderTemplate.model_validate(
        {
            **get_provider_template("openai").model_dump(),
            "provider_type": "openai-compatible",
            "auth_driver": "browser-fixture",
            "credential_env": None,
            **overrides,
        }
    )


def test_template_creates_instance_id_and_update_preserves_auth_choice(tmp_path, fixture_product):
    manager = ProviderConfigManager(tmp_path)
    template = subscription_template()
    first = manager.create_from_template(template, provider_id="first")
    second = manager.create_from_template(template, provider_id="second")
    assert first.credential_ref != second.credential_ref
    assert first.auth_driver == "browser-fixture"
    assert first.template.auth_driver == "browser-fixture"
    assert first.template.credential_env is None
    next_template = subscription_template(
        auth_driver="static", credential_env="NEW_KEY", revision=template.revision + 1
    )
    updated = manager.update_from_template(next_template, provider_id="first")
    assert updated.provider.auth_driver == first.auth_driver
    assert updated.provider.credential_ref == first.credential_ref
    assert "auth_driver" in updated.preserved_fields
    repeated = manager.update_from_template(next_template, provider_id="first")
    assert repeated.changed is False
    assert repeated.provider.credential_ref == first.credential_ref
    replacement = manager.replace_from_template(next_template, provider_id="first")
    assert replacement.auth_driver == "static"
    assert replacement.credential_ref == CredentialRef.parse("env:NEW_KEY")


def test_template_auth_override_is_explicit_and_update_does_not_switch_it(
    tmp_path, fixture_product
):
    manager = ProviderConfigManager(tmp_path)
    static = subscription_template(auth_driver="static", credential_env="ORIGINAL_KEY")
    original = manager.create_from_template(static, auth_driver="browser-fixture")
    updated = manager.update_from_template(
        subscription_template(
            auth_driver="static", credential_env="NEW_KEY", revision=static.revision + 1
        )
    )
    assert updated.provider.auth_driver == original.auth_driver
    assert updated.provider.credential_ref == original.credential_ref


def test_managed_template_rejects_api_key_and_static_template_may_omit_env(
    tmp_path, fixture_product
):
    with pytest.raises(ValidationError, match="credential_env"):
        subscription_template(credential_env="KEY")
    manager = ProviderConfigManager(tmp_path)
    with pytest.raises(ConfigError, match="API Key"):
        manager.create_from_template(subscription_template(), api_key="fixture-secret")
    assert not manager.config_path.exists()
    static = manager.create_from_template(subscription_template(auth_driver="static"))
    assert static.credential_ref.type == "none"


def test_noncurrent_template_schema_is_not_migrated_or_rewritten(tmp_path):
    store = ProviderTemplateStore(tmp_path)
    current = store.catalog()
    path = current.config_path
    old = json.loads(path.read_text(encoding="utf-8"))
    old["schema_version"] = SCHEMA_VERSION - 1
    path.write_text(json.dumps(old), encoding="utf-8")
    before = path.read_bytes()
    snapshot = store.catalog()
    assert any("unsupported provider template schema" in notice for notice in snapshot.notices)
    assert path.read_bytes() == before
