from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ouro.config.providers import ProviderConfigManager
from ouro.core.errors import ConfigError, UnsupportedDataVersionError
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
    (tmp_path / "config/providers.json").write_bytes(
        (FIXTURES / "records/providers-vision.json").read_bytes()
    )
    return tmp_path


def test_legacy_provider_config_is_a_recoverable_version_error(provider_home: Path) -> None:
    with pytest.raises(ConfigError) as error:
        ProviderConfigManager(provider_home).snapshot()
    assert isinstance(error.value, UnsupportedDataVersionError)


def test_provider_upgrade_preserves_configuration_and_is_idempotent(provider_home: Path) -> None:
    path = provider_home / "config/providers.json"
    before = json.loads(path.read_text("utf-8"))
    original_bytes = path.read_bytes()
    assert upgrade(provider_home, check=True)["status"] == "upgrade_required"
    assert path.read_bytes() == original_bytes
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
        for key in source.keys() - {"supports_vision"}:
            assert endpoint[key] == source[key]
    snapshot = ProviderConfigManager(provider_home).snapshot(include_builtin=False)
    assert snapshot.default_provider_id == "provider_fixture"
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
    endpoint = raw["providers"]["items"]["provider_fixture"]["endpoints"]["image"]
    if damage == "invalid_flag":
        endpoint["supports_vision"] = "fixture-not-a-real-secret"
    elif damage == "mixed_fields":
        endpoint["supports_image_input"] = False
    elif damage == "unknown_field":
        endpoint["unknown"] = "fixture-not-a-real-secret"
    else:
        raw["providers"]["default"] = "missing"
    path.write_text(json.dumps(raw), encoding="utf-8")
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
    path.write_text(json.dumps(raw), encoding="utf-8")
    upgrade(provider_home)
    after = json.loads(path.read_text("utf-8"))
    assert after["providers"]["items"]["provider_fixture"]["endpoints"]["text"] == endpoint


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
