import json

import pytest

from ouro.config import (
    ConfigService,
    CredentialRef,
    CredentialResolver,
    PromptConfigManager,
    ProviderConfigManager,
    ProviderRuntimeOptions,
)
from ouro.core.errors import ConfigError, CredentialError
from ouro.prompts import DEFAULT_SYSTEM_PROMPT


def test_credential_ref_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OURO_TEST_KEY", "secret")
    ref = CredentialRef.parse("env:OURO_TEST_KEY")

    assert ref.display() == "env:OURO_TEST_KEY"
    assert CredentialResolver().resolve(ref) == "secret"


def test_missing_credential_raises_redacted_error() -> None:
    ref = CredentialRef.parse("env:OURO_MISSING_KEY")

    with pytest.raises(CredentialError) as exc:
        CredentialResolver().resolve(ref)

    assert "env:OURO_MISSING_KEY" in str(exc.value)


def test_config_service_precedence() -> None:
    service = ConfigService(
        {"model": {"name": "runtime"}},
        {"model": {"temperature": 0.2}},
        {"model": {"name": "default", "temperature": 1.0}},
    )

    assert service.get("model.name") == "runtime"
    assert service.get("model.temperature") == 0.2


def test_provider_config_manager_resolves_configured_default(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "glm",
                    "items": {
                        "glm": {
                            "provider_id": "glm",
                            "provider_type": "openai-compatible",
                            "default_model": "glm-test",
                            "base_url": "https://example.test/v1",
                            "credential_ref": "env:OURO_TEST_KEY",
                            "timeout_seconds": 7,
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    options = ProviderConfigManager(tmp_path).resolve_runtime_options(
        ProviderRuntimeOptions(),
    )

    assert options.provider == "openai-compatible"
    assert options.provider_id == "glm"
    assert options.model == "glm-test"
    assert options.base_url == "https://example.test/v1"
    assert options.credential_ref is not None
    assert options.credential_ref.display() == "env:OURO_TEST_KEY"
    assert options.timeout == 7


def test_prompt_config_manager_persists_system_prompt(tmp_path) -> None:
    manager = PromptConfigManager(tmp_path)

    assert manager.resolve_system_prompt() == DEFAULT_SYSTEM_PROMPT

    saved = manager.set_system_prompt("Custom prompt.")

    assert saved.system_prompt == "Custom prompt."
    assert manager.resolve_system_prompt() == "Custom prompt."
    assert json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))[
        "prompts"
    ] == {"system": "Custom prompt."}

    cleared = manager.clear_system_prompt()

    assert cleared.system_prompt is None
    assert manager.resolve_system_prompt() == DEFAULT_SYSTEM_PROMPT


def test_provider_config_manager_selects_configured_provider_by_id(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "faux",
                    "items": {
                        "glm": {
                            "provider_id": "glm",
                            "provider_type": "openai-compatible",
                            "default_model": "glm-test",
                            "base_url": "https://example.test/v1",
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    options = ProviderConfigManager(tmp_path).resolve_runtime_options(
        ProviderRuntimeOptions(provider_id="glm", model="override-model"),
    )

    assert options.provider == "openai-compatible"
    assert options.provider_id == "glm"
    assert options.model == "override-model"
    assert options.base_url == "https://example.test/v1"


def test_provider_config_manager_rejects_provider_id_mismatch(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "local",
                    "items": {
                        "local": {
                            "provider_id": "glm",
                            "provider_type": "openai-compatible",
                            "default_model": "glm-test",
                            "base_url": "https://example.test/v1",
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="provider_id must match"):
        ProviderConfigManager(tmp_path).snapshot()
