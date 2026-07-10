import json

import pytest

from ouro.config import (
    ConfigService,
    CredentialRef,
    CredentialResolver,
    PromptConfigManager,
    ProviderConfigManager,
    ProviderEndpointConfig,
    ProviderRuntimeOptions,
    endpoint_config_to_public_dict,
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


def test_endpoint_public_output_redacts_sensitive_extension_values() -> None:
    public = endpoint_config_to_public_dict(
        ProviderEndpointConfig(
            endpoint_id="chat",
            model_id="chat-model",
            request_options={
                "api_key": "secret-value",
                "nested": {"password": "password-value"},
                "seed": 7,
            },
        )
    )

    assert public["request_options"] == {
        "api_key": "***",
        "nested": {"password": "***"},
        "seed": 7,
    }


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


def test_provider_config_resolves_endpoint_capabilities(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "glm",
                    "items": {
                        "glm": {
                            "provider_type": "openai-compatible",
                            "credential_ref": "env:OURO_TEST_KEY",
                            "provider_options": {"seed": 1, "top_p": 0.8},
                            "default_endpoint": "chat",
                            "endpoints": {
                                "chat": {
                                    "model_id": "glm-chat",
                                    "base_url": "https://chat.example.test/v1",
                                },
                                "reasoning": {
                                    "model_id": "glm-reasoning",
                                    "base_url": "https://reasoning.example.test/v1",
                                    "temperature": 0.2,
                                    "context_window": 200_000,
                                    "max_output_tokens": 16_000,
                                    "supports_tools": False,
                                    "supports_vision": True,
                                    "supports_streaming": False,
                                    "supports_reasoning": True,
                                    "supported_thinking_levels": ["low", "high"],
                                    "default_thinking_level": "high",
                                    "request_options": {"seed": 7},
                                },
                            },
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    options = ProviderConfigManager(tmp_path).resolve_runtime_options(
        ProviderRuntimeOptions(provider_id="glm", endpoint_id="reasoning")
    )

    assert options.endpoint_id == "reasoning"
    assert options.model == "glm-reasoning"
    assert options.base_url == "https://reasoning.example.test/v1"
    assert options.temperature == 0.2
    assert options.context_window == 200_000
    assert options.max_output_tokens == 16_000
    assert options.supports_tools is False
    assert options.supports_vision is True
    assert options.supports_streaming is False
    assert options.supports_reasoning is True
    assert options.supported_thinking_levels == ("low", "high")
    assert options.thinking_level == "high"
    assert options.extra == {"seed": 7, "top_p": 0.8}

    disabled_reasoning = ProviderConfigManager(tmp_path).resolve_runtime_options(
        ProviderRuntimeOptions(
            provider_id="glm",
            endpoint_id="reasoning",
            supports_reasoning=False,
        )
    )
    assert disabled_reasoning.supports_reasoning is False
    assert disabled_reasoning.supported_thinking_levels == ()
    assert disabled_reasoning.thinking_level is None


def test_provider_endpoint_writes_new_shape_and_validates_thinking_levels(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    manager.upsert_provider(
        "glm",
        provider_type="openai-compatible",
        api_key_env="OURO_TEST_KEY",
    )

    endpoint = manager.upsert_endpoint(
        "glm",
        "reasoning",
        model_id="glm-reasoning",
        base_url="https://example.test/v1",
        supports_reasoning=True,
        supported_thinking_levels=("low", "high"),
        default_thinking_level="high",
        make_default=True,
    )

    assert endpoint.endpoint_id == "reasoning"
    raw = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))
    provider_raw = raw["providers"]["items"]["glm"]
    assert "default_model" not in provider_raw
    assert "base_url" not in provider_raw
    assert provider_raw["default_endpoint"] == "reasoning"
    assert provider_raw["endpoints"]["reasoning"]["model_id"] == "glm-reasoning"

    with pytest.raises(ConfigError, match="default_thinking_level"):
        manager.upsert_endpoint(
            "glm",
            "reasoning",
            supported_thinking_levels=("low",),
            default_thinking_level="high",
        )


def test_provider_and_endpoint_mutations_translate_validation_errors(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)

    with pytest.raises(ConfigError, match="timeout_seconds"):
        manager.upsert_provider(
            "glm",
            provider_type="openai-compatible",
            timeout_seconds=-1,
        )

    manager.upsert_provider("glm", provider_type="openai-compatible")
    with pytest.raises(ConfigError, match="model_id"):
        manager.upsert_endpoint(
            "glm",
            "chat",
            model_id="   ",
            base_url="https://example.test/v1",
        )

    with pytest.raises(ConfigError, match="could not serialize"):
        manager.upsert_endpoint(
            "glm",
            "chat",
            model_id="glm-chat",
            base_url="https://example.test/v1",
            request_options={"invalid": object()},
        )


def test_incomplete_provider_does_not_replace_safe_default(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)

    provider = manager.upsert_provider("glm", provider_type="openai-compatible")

    assert provider.default_endpoint is None
    assert manager.snapshot().default_provider_id == "faux"
    with pytest.raises(ConfigError, match="default provider"):
        manager.upsert_provider(
            "glm",
            provider_type="openai-compatible",
            make_default=True,
        )

    with pytest.raises(ConfigError, match="built-in provider"):
        manager.upsert_provider("faux", provider_type="faux")


def test_runtime_resolution_distinguishes_configured_and_ad_hoc_providers(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)

    with pytest.raises(ConfigError, match="provider not found"):
        manager.resolve_runtime_options(ProviderRuntimeOptions(provider_id="missing"))

    options = manager.resolve_runtime_options(
        ProviderRuntimeOptions(
            provider="openai-compatible",
            model="ad-hoc-model",
            base_url="https://example.test/v1",
        )
    )

    assert options.provider == "openai-compatible"
    assert options.provider_id is None
    assert options.endpoint_id is None


def test_raw_provider_timeout_zero_is_rejected_and_enabled_is_typed(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "items": {
                        "glm": {
                            "provider_type": "openai-compatible",
                            "enabled": "false",
                            "timeout_seconds": 10,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    provider = ProviderConfigManager(tmp_path).get_provider("glm")
    assert provider is not None
    assert provider.enabled is False

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["providers"]["items"]["glm"]["timeout_seconds"] = 0
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="timeout_seconds"):
        ProviderConfigManager(tmp_path).snapshot()


def test_provider_mutations_keep_default_provider_runnable(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    manager.upsert_provider(
        "glm",
        provider_type="openai-compatible",
        default_model="glm-test",
        base_url="https://example.test/v1",
        make_default=True,
    )
    assert manager.snapshot().default_provider_id == "glm"

    manager.remove_endpoint("glm", "default")
    assert manager.snapshot().default_provider_id == "faux"

    manager.upsert_endpoint(
        "glm",
        "chat",
        model_id="glm-test",
        base_url="https://example.test/v1",
        make_default=True,
    )
    manager.set_default_provider("glm")
    manager.upsert_provider("glm", enabled=False)
    assert manager.snapshot().default_provider_id == "faux"


@pytest.mark.parametrize(
    "providers_section",
    [[], {"items": "invalid"}, {"items": 0}, {"default": 0, "items": {}}],
)
def test_invalid_provider_registry_shapes_fail_visibly(tmp_path, providers_section) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"providers": providers_section}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="providers"):
        ProviderConfigManager(tmp_path).snapshot()


def test_unknown_explicit_default_provider_fails_visibly(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"providers": {"default": "missing", "items": {}}}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="default provider not found"):
        ProviderConfigManager(tmp_path).snapshot()
