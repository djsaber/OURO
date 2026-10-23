from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ouro.config.provider_templates import get_provider_template
from ouro.config.providers.codec import provider_config_from_mapping
from ouro.config.providers.manager import ProviderConfigManager
from ouro.config.providers.template_store import (
    SCHEMA_VERSION,
    ProviderTemplateStore,
    template_fingerprint,
)
from ouro.config.reasoning import EndpointReasoningConfig
from ouro.core.errors import ConfigError, UnsupportedDataVersionError
from ouro.core.provider import ModelMetadata
from ouro.core.reasoning import ReasoningRequest
from ouro.providers.reasoning import ReasoningPayloadEncoder
from ouro.tui.app import OuroTuiApp
from ouro.tui.recovery import DataRecoveryApp
from ouro_upgrade import configuration, workspace
from ouro_upgrade.engine import upgrade
from ouro_upgrade.errors import UpgradeError
from tests.upgrade.conftest import FIXTURES
from tests.upgrade.test_recovery import wait_until
from tests.upgrade.test_upgrade import hashes


@pytest.fixture
def provider_home(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    raw = json.loads((FIXTURES / "records/providers-vision.json").read_text("utf-8"))
    reasoning = json.loads((FIXTURES / "records/providers-reasoning-v030.json").read_text("utf-8"))
    raw["providers"]["items"].update(reasoning["providers"]["items"])
    (tmp_path / "config/providers.json").write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "config/provider-templates.json").write_bytes(
        (FIXTURES / "records/provider-templates-reasoning-v030.json").read_bytes()
    )
    return tmp_path


def test_legacy_provider_config_is_a_recoverable_version_error(provider_home: Path) -> None:
    with pytest.raises(ConfigError) as error:
        ProviderConfigManager(provider_home).snapshot()
    assert isinstance(error.value, UnsupportedDataVersionError)
    raw = json.loads((provider_home / "config/providers.json").read_text("utf-8"))
    with pytest.raises(UnsupportedDataVersionError):
        provider_config_from_mapping(
            "reasoning_fixture", raw["providers"]["items"]["reasoning_fixture"]
        )
    # Even an otherwise valid unsupported capability must not silently lose a legacy field.
    with pytest.raises(UnsupportedDataVersionError):
        EndpointReasoningConfig.model_validate({"capability": {"default_effort": None}})
    templates_path = provider_home / "config/provider-templates.json"
    templates = json.loads(templates_path.read_text("utf-8"))
    before = templates_path.read_bytes()
    catalog = ProviderTemplateStore(provider_home).catalog()
    assert catalog.entries
    assert json.loads(templates_path.read_text("utf-8"))["schema_version"] == SCHEMA_VERSION
    backup = next(templates_path.parent.glob("provider-templates.backup-v4-*.json"))
    assert backup.read_bytes() == before
    # Invalid entries in a current catalog are isolated by the template reader.
    templates["schema_version"] = SCHEMA_VERSION
    templates_path.write_text(json.dumps(templates), encoding="utf-8")
    catalog = ProviderTemplateStore(provider_home).catalog()
    assert any("reasoning-fixture' skipped" in notice for notice in catalog.notices)
    assert not any("ouro-upgrade" in notice for notice in catalog.notices)
    assert (
        json.loads(templates_path.read_text("utf-8"))["templates"]["reasoning-fixture"]
        == templates["templates"]["reasoning-fixture"]
    )


def test_provider_upgrade_preserves_configuration_and_is_idempotent(provider_home: Path) -> None:
    path = provider_home / "config/providers.json"
    before = json.loads(path.read_text("utf-8"))
    original_bytes = path.read_bytes()
    templates_path = provider_home / "config/provider-templates.json"
    original_templates = templates_path.read_bytes()
    assert upgrade(provider_home, check=True)["status"] == "upgrade_required"
    assert path.read_bytes() == original_bytes
    assert templates_path.read_bytes() == original_templates
    report = upgrade(provider_home)
    assert report["status"] == "upgraded"
    assert report["files"] == ["config/providers.json"]
    after = json.loads(path.read_text("utf-8"))
    provider = after["providers"]["items"]["provider_fixture"]
    original = before["providers"]["items"]["provider_fixture"]
    for key in original.keys() - {"endpoints"}:
        assert provider[key] == original[key]
    for name, endpoint in provider["endpoints"].items():
        source = original["endpoints"][name]
        assert "supports_vision" not in endpoint
        assert endpoint["supports_image_input"] is source["supports_vision"]
        assert endpoint["supports_audio_input"] is False
        assert endpoint["supports_video_input"] is False
        for key in source.keys() - {"supports_vision", "reasoning"}:
            assert endpoint[key] == source[key]
        assert not {"output", "continuation"} & endpoint["reasoning"]["capability"].keys()
    snapshot = ProviderConfigManager(provider_home).snapshot(include_builtin=False)
    assert snapshot.default_provider_id == "provider_fixture"
    source = before["providers"]["items"]["reasoning_fixture"]
    converted = after["providers"]["items"]["reasoning_fixture"]
    parsed = provider_config_from_mapping("reasoning_fixture", converted)
    for key in source.keys() - {"endpoints", "template"}:
        assert converted[key] == source[key]
    expected_defaults = {"inherit": "default", "selected": "enabled", "disabled": "disabled"}
    default_payloads = {
        "local-inherit": {"reasoning": {"summary": "auto"}},
        "local-selected": {"reasoning": {"effort": "high", "summary": "auto"}},
        "local-disabled": {"reasoning": {"effort": "none"}},
    }
    for endpoint in parsed.endpoints:
        endpoint_id = endpoint.endpoint_id
        original_endpoint = source["endpoints"][endpoint_id]
        result = converted["endpoints"][endpoint_id]
        for key in original_endpoint.keys() - {"reasoning"}:
            assert result[key] == original_endpoint[key]
        capability = endpoint.reasoning.capability
        assert capability.supported_summaries == ()
        assert capability.default.summary is None
        if endpoint.reasoning.profile is not None:
            assert (
                endpoint.reasoning.profile.effort
                == original_endpoint["reasoning"]["profile"]["variants"]
            )
            assert endpoint.reasoning.profile.summary == {}
            assert "default_effort" not in result["reasoning"]["capability"]
            assert capability.default.effort == "high"
            assert capability.default.mode == expected_defaults[endpoint_id.removeprefix("local-")]
            for request, expected in (
                (ReasoningRequest(), default_payloads[endpoint_id]),
                (
                    ReasoningRequest(mode="enabled"),
                    {"reasoning": {"effort": "high", "summary": "auto"}},
                ),
                (ReasoningRequest(mode="disabled"), {"reasoning": {"effort": "none"}}),
                (
                    ReasoningRequest(effort="low"),
                    {"reasoning": {"effort": "low", "summary": "auto"}},
                ),
            ):
                payload = {}
                ReasoningPayloadEncoder(endpoint.reasoning.profile).apply(
                    payload,
                    ModelMetadata(
                        provider_id="fixture",
                        model_id=endpoint.model_id,
                        reasoning=capability,
                        reasoning_request=request,
                    ),
                )
                assert payload == expected
    for old, new in zip(
        source["template"]["endpoints"], converted["template"]["endpoints"], strict=True
    ):
        endpoint = parsed.endpoint(new["configured_endpoint_id"])
        assert endpoint is not None
        business = endpoint.model_dump(mode="json")
        business["endpoint_id"] = new["template_endpoint_id"]
        if new["configured_endpoint_id"] == "local-selected":
            assert new == old
            assert new["fingerprint"] != template_fingerprint(business)
        else:
            assert new == {**old, "fingerprint": template_fingerprint(business)}
            assert new["fingerprint"] != old["fingerprint"]
    assert templates_path.read_bytes() == original_templates
    manifest = json.loads((provider_home / report["manifest"]).read_text("utf-8"))
    assert len(manifest["changes"]) == 1
    assert (provider_home / report["backup"] / "0").read_bytes() == original_bytes
    upgraded = hashes(provider_home)
    assert upgrade(provider_home)["status"] == "current"
    assert hashes(provider_home) == upgraded


@pytest.mark.parametrize("damage", ["invalid_flag", "mixed_fields", "unknown_field", "default"])
def test_invalid_provider_config_prevents_writes_without_exposing_secrets(
    provider_home: Path, damage: str
) -> None:
    path = provider_home / "config/providers.json"
    raw = json.loads(path.read_text("utf-8"))
    reasoning_raw = copy.deepcopy(raw)
    endpoint = raw["providers"]["items"]["provider_fixture"]["endpoints"]["image"]
    if damage == "invalid_flag":
        endpoint["supports_vision"] = "fixture-not-a-real-secret"
    elif damage == "mixed_fields":
        endpoint["supports_image_input"] = False
    elif damage == "unknown_field":
        endpoint["unknown"] = "fixture-not-a-real-secret"
    else:
        raw["providers"]["default"] = "missing"
    reasoning = reasoning_raw["providers"]["items"]["reasoning_fixture"]["endpoints"][
        "local-inherit"
    ]["reasoning"]
    if damage == "invalid_flag":
        reasoning["capability"]["default_effort"] = "fixture-not-a-real-secret"
    elif damage == "mixed_fields":
        reasoning["profile"]["summary"] = {"auto": {"reasoning": {"summary": "auto"}}}
    elif damage == "unknown_field":
        reasoning["profile"]["variants"]["unknown"] = {"reasoning": "fixture-not-a-real-secret"}
    else:
        reasoning["capability"]["default"]["effort"] = "low"
    for invalid in (raw, reasoning_raw):
        path.write_text(json.dumps(invalid), encoding="utf-8")
        before = hashes(provider_home)
        with pytest.raises(UpgradeError) as error:
            upgrade(provider_home)
        assert "fixture-not-a-real-secret" not in str(error.value)
        assert hashes(provider_home) == before
        assert not (provider_home / ".upgrade/in-progress.json").exists()


def test_interrupted_provider_upgrade_restores_original_bytes(provider_home, monkeypatch) -> None:
    before = hashes(provider_home)
    original = workspace.install

    def interrupt(source, target, mode, **kwargs):
        original(source, target, mode, **kwargs)
        raise OSError("interrupted after configuration replacement")

    monkeypatch.setattr(workspace, "install", interrupt)
    with pytest.raises(UpgradeError):
        upgrade(provider_home)
    monkeypatch.setattr(workspace, "install", original)
    assert upgrade(provider_home, restore=True)["status"] == "restored"
    assert hashes(provider_home) == before


@pytest.mark.parametrize("historical_home", ["files", "sqlite"], indirect=True)
def test_providers_upgrade_together_with_core_data(historical_home: Path) -> None:
    home = historical_home
    (home / "config").mkdir()
    (home / "config/providers.json").write_bytes(
        (FIXTURES / "records/providers-vision.json").read_bytes()
    )
    backend = "sqlite" if (home / "sessions.sqlite3").exists() else "files"
    options = {"session_backend": backend, "task_backend": backend}
    assert upgrade(home, **options)["status"] == "upgraded"
    assert ProviderConfigManager(home).snapshot().default_provider_id == "provider_fixture"
    assert upgrade(home, **options)["status"] == "current"


def test_mixed_endpoint_formats_preserve_current_capabilities(provider_home: Path) -> None:
    path = provider_home / "config/providers.json"
    raw = json.loads(path.read_text("utf-8"))
    provider = raw["providers"]["items"]["provider_fixture"]
    provider["protocol"] = "openai-chat-completions"
    endpoint = provider["endpoints"]["text"]
    endpoint.pop("supports_vision")
    endpoint.update(
        supports_image_input=False, supports_audio_input=True, supports_video_input=True
    )
    endpoint["reasoning"]["capability"].pop("output")
    endpoint["reasoning"]["capability"].pop("continuation")
    reasoning_provider = configuration.convert_providers(
        json.loads((FIXTURES / "records/providers-reasoning-v030.json").read_text("utf-8"))
    )["providers"]["items"]["reasoning_fixture"]
    raw["providers"]["items"]["reasoning_fixture"] = reasoning_provider
    path.write_text(json.dumps(raw), encoding="utf-8")
    upgrade(provider_home)
    after = json.loads(path.read_text("utf-8"))
    assert after["providers"]["items"]["provider_fixture"]["endpoints"]["text"] == endpoint
    assert after["providers"]["items"]["reasoning_fixture"] == reasoning_provider


@pytest.mark.parametrize("modified", [False, True])
def test_removing_response_controls_preserves_endpoint_binding_ownership(tmp_path, modified):
    manager = ProviderConfigManager(tmp_path)
    created = manager.create_from_template(get_provider_template("deepseek"))
    raw = json.loads((tmp_path / "config/providers.json").read_text("utf-8"))
    provider = raw["providers"]["items"][created.provider_id]
    for binding in provider["template"]["endpoints"]:
        endpoint = provider["endpoints"][binding["configured_endpoint_id"]]
        endpoint["reasoning"]["capability"].update(output="text", continuation="text")
        business = created.endpoint(binding["configured_endpoint_id"]).model_dump(mode="json")
        business["endpoint_id"] = binding["template_endpoint_id"]
        business["reasoning"]["capability"].update(output="text", continuation="text")
        binding["fingerprint"] = template_fingerprint(business)
        if modified:
            endpoint["context_window"] += 1
    original = copy.deepcopy(raw)
    with pytest.raises(UnsupportedDataVersionError):
        provider_config_from_mapping(created.provider_id, provider)
    converted = configuration.convert_providers(raw)
    assert raw == original
    assert configuration.convert_providers(converted) == converted
    value = converted["providers"]["items"][created.provider_id]
    parsed = provider_config_from_mapping(created.provider_id, value)
    for old, binding in zip(
        provider["template"]["endpoints"], value["template"]["endpoints"], strict=True
    ):
        endpoint = parsed.endpoint(binding["configured_endpoint_id"])
        business = endpoint.model_dump(mode="json")
        business["endpoint_id"] = binding["template_endpoint_id"]
        assert binding["fingerprint"] == (
            old["fingerprint"] if modified else template_fingerprint(business)
        )
        assert (
            not {"output", "continuation"}
            & value["endpoints"][endpoint.endpoint_id]["reasoning"]["capability"].keys()
        )


async def test_tui_routes_legacy_config_to_offline_upgrade(provider_home: Path) -> None:
    app = OuroTuiApp(home=provider_home)
    async with app.run_test(size=(100, 32)):
        await wait_until(lambda: app.startup_data_error is not None)
    assert isinstance(app.startup_data_error, UnsupportedDataVersionError)
    recovery = DataRecoveryApp(
        home=provider_home, error=str(app.startup_data_error), auto_upgrade=True
    )
    async with recovery.run_test(size=(100, 32)):
        await wait_until(lambda: recovery.return_value == "upgraded")
    assert ProviderConfigManager(provider_home).snapshot().default_provider_id == "provider_fixture"


def test_unversioned_configuration_can_compose_structural_transitions(monkeypatch) -> None:
    raw = json.loads((FIXTURES / "records/providers-vision.json").read_text("utf-8"))
    endpoint = raw["providers"]["items"]["provider_fixture"]["endpoints"]["image"]
    endpoint["synthetic_legacy_vision"] = endpoint.pop("supports_vision")
    before = copy.deepcopy(raw)

    def earlier_shape(value: dict) -> None:
        if "synthetic_legacy_vision" in value:
            value["supports_vision"] = value.pop("synthetic_legacy_vision")

    monkeypatch.setattr(
        configuration, "ENDPOINT_STEPS", (earlier_shape, *configuration.ENDPOINT_STEPS)
    )
    result = configuration.convert_providers(raw)
    upgraded = result["providers"]["items"]["provider_fixture"]["endpoints"]["image"]
    assert upgraded["supports_image_input"] is True
    assert "synthetic_legacy_vision" not in upgraded
    assert raw == before
    assert configuration.convert_providers(result) == result
