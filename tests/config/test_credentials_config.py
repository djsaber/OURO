import json

import pytest

from ouro.config import (
    CredentialRef,
    CredentialResolver,
    ProviderConfig,
    ProviderConfigManager,
    ProviderEndpointConfig,
    ProviderRuntimeOptions,
    endpoint_config_to_public_dict,
    mapped_reasoning,
    openai_chat_completions_effort_reasoning,
    provider_config_to_public_dict,
)
from ouro.config.endpoints import endpoint_config_from_mapping
from ouro.config.provider_models import default_api_path
from ouro.core.errors import ConfigError, CredentialError
from ouro.core.provider import ProviderRetryPolicy
from ouro.core.reasoning import ReasoningRequest, resolve_reasoning


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
            reasoning=mapped_reasoning(
                name="private-native-config",
                activation="toggle",
                variants={
                    "enabled": {"thinking": {"type": "enabled", "password": "secret"}},
                    "disabled": {"thinking": {"type": "disabled"}},
                },
            ),
        )
    )

    assert public["request_options"] == {
        "api_key": "***",
        "nested": {"password": "***"},
        "seed": 7,
    }
    assert public["reasoning"]["profile"]["variants"]["enabled"]["thinking"]["password"] == "***"


def test_provider_options_are_redacted_in_public_output() -> None:
    public = provider_config_to_public_dict(
        ProviderConfig(
            provider_id="custom",
            display_name="Custom",
            provider_type="custom",
            protocol="openai-chat-completions",
            extra={
                "api_key": "secret-value",
                "nested": {"password": "password-value"},
                "top_p": 0.8,
            },
        )
    )

    assert public["provider_options"] == {
        "api_key": "***",
        "nested": {"password": "***"},
        "top_p": 0.8,
    }


def test_provider_options_are_replaceable_defaults_with_owned_field_validation(
    tmp_path,
) -> None:
    manager = ProviderConfigManager(tmp_path)
    created = manager.create_provider(
        display_name="Custom",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.test/v1",
        provider_options={"tool_choice": "none", "top_p": 0.8},
    )

    assert created.extra == {"tool_choice": "none", "top_p": 0.8}
    provider_id = created.provider_id
    assert provider_id.startswith("provider_")
    assert manager.update_provider(provider_id, timeout_seconds=30).extra == created.extra

    endpoint = manager.create_endpoint(
        provider_id,
        model_id="custom-model",
    )
    assert endpoint.supports_tools is True
    assert endpoint.supports_structured_output is True

    updated = manager.update_provider(
        provider_id,
        provider_options={"top_p": 0.9},
    )
    assert updated.extra == {"top_p": 0.9}
    assert manager.update_provider(provider_id, provider_options={}).extra == {}

    with pytest.raises(ConfigError, match="protocol-owned fields: model"):
        manager.update_provider(
            provider_id,
            provider_options={"model": "wrong-model"},
        )
    configured = manager.get_provider(provider_id)
    assert configured is not None
    assert configured.extra == {}


def test_provider_config_manager_resolves_configured_default(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "glm",
                    "items": {
                        "glm": {
                            "display_name": "GLM",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
                            "credential_ref": "env:OURO_TEST_KEY",
                            "timeout_seconds": 7,
                            "default_endpoint": "default",
                            "endpoints": {
                                "default": {
                                    "model_id": "glm-test",
                                    "base_url": "https://example.test/v1",
                                }
                            },
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

    assert options.protocol == "openai-chat-completions"
    assert options.provider_id == "glm"
    assert options.model == "glm-test"
    assert options.base_url == "https://example.test/v1"
    assert options.credential_ref is not None
    assert options.credential_ref.display() == "env:OURO_TEST_KEY"
    assert options.timeout == 7


def test_provider_config_manager_selects_configured_provider_by_id(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "faux",
                    "items": {
                        "glm": {
                            "display_name": "GLM",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
                            "default_endpoint": "default",
                            "endpoints": {
                                "default": {
                                    "model_id": "glm-test",
                                    "base_url": "https://example.test/v1",
                                }
                            },
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

    assert options.protocol == "openai-chat-completions"
    assert options.provider_id == "glm"
    assert options.model == "override-model"
    assert options.base_url == "https://example.test/v1"


def test_provider_config_manager_rejects_user_configured_provider_id(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "local",
                    "items": {
                        "local": {
                            "provider_id": "glm",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
                            "base_url": "https://example.test/v1",
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unsupported provider config fields: provider_id"):
        ProviderConfigManager(tmp_path).snapshot()


def test_provider_config_manager_requires_display_name(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "items": {
                        "provider_generated": {
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="missing display_name"):
        ProviderConfigManager(tmp_path).snapshot()


def test_provider_config_rejects_noncanonical_provider_type(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "items": {
                        "local": {
                            "display_name": "Local",
                            "provider_type": "openai_chat_completions",
                            "protocol": "openai-chat-completions",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unsupported provider type"):
        ProviderConfigManager(tmp_path).snapshot()


def test_provider_config_requires_explicit_protocol_without_legacy_inference(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "items": {
                        "local": {
                            "display_name": "Local",
                            "provider_type": "custom",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unsupported provider protocol: None"):
        ProviderConfigManager(tmp_path).snapshot()


def test_provider_type_protocol_combinations_are_validated() -> None:
    with pytest.raises(ValueError, match="not supported by provider type"):
        ProviderConfig(
            provider_id="openai",
            display_name="OpenAI",
            provider_type="openai",
            protocol="anthropic-messages",
        )


@pytest.mark.parametrize(
    "field",
    (
        "model",
        "endpoint",
        "endpoint_id",
        "supports_multimodal",
        "supports_reasoning",
        "supported_thinking_levels",
        "default_thinking_level",
        "thinking_levels",
        "default_reasoning_level",
        "extra",
    ),
)
def test_endpoint_config_rejects_noncanonical_fields(field) -> None:
    with pytest.raises(ValueError, match="unsupported endpoint config fields"):
        endpoint_config_from_mapping("chat", {"model_id": "model", field: "value"})


@pytest.mark.parametrize(
    "raw",
    (
        {"provider": {}},
        {"providers": {"items": []}},
        {"providers": {"items": {}, "unknown": True}},
        {
            "providers": {
                "items": {
                    "local": {
                        "display_name": "Local",
                        "provider_type": "custom",
                        "protocol": "openai-chat-completions",
                        "default_model": "model",
                    }
                }
            }
        },
    ),
)
def test_provider_config_rejects_noncanonical_shapes(tmp_path, raw) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="unsupported|must be an object"):
        ProviderConfigManager(tmp_path).snapshot()


def test_provider_config_resolves_endpoint_capabilities(tmp_path) -> None:
    reasoning = openai_chat_completions_effort_reasoning(
        supported_efforts=("low", "high"),
        default_effort="high",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "glm",
                    "items": {
                        "glm": {
                            "display_name": "GLM",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
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
                                    "reasoning": reasoning.model_dump(mode="json"),
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
    assert options.reasoning == reasoning
    assert options.reasoning_request is None
    assert resolve_reasoning(reasoning.capability, options.reasoning_request).effective == (
        ReasoningRequest(mode="enabled", effort="high")
    )
    assert options.extra == {"seed": 7, "top_p": 0.8}

    disabled_reasoning = ProviderConfigManager(tmp_path).resolve_runtime_options(
        ProviderRuntimeOptions(
            provider_id="glm",
            endpoint_id="reasoning",
            reasoning_request=ReasoningRequest(mode="disabled"),
        )
    )
    assert disabled_reasoning.reasoning == reasoning
    assert disabled_reasoning.reasoning_request == ReasoningRequest(mode="disabled")


def test_provider_endpoint_writes_reasoning_profile_and_rejects_option_conflicts(
    tmp_path,
) -> None:
    manager = ProviderConfigManager(tmp_path)
    provider = manager.create_provider(
        display_name="GLM",
        provider_type="custom",
        protocol="openai-chat-completions",
        api_key_env="OURO_TEST_KEY",
    )

    reasoning = openai_chat_completions_effort_reasoning(
        supported_efforts=("low", "high"),
        default_effort="high",
    )
    endpoint = manager.create_endpoint(
        provider.provider_id,
        display_name="Reasoning",
        model_id="glm-reasoning",
        base_url="https://example.test/v1",
        reasoning=reasoning,
        make_default=True,
    )

    assert endpoint.endpoint_id.startswith("endpoint_")
    raw = json.loads((tmp_path / "config" / "providers.json").read_text(encoding="utf-8"))
    provider_raw = raw["providers"]["items"][provider.provider_id]
    assert "provider_id" not in provider_raw
    assert "default_model" not in provider_raw
    assert "base_url" not in provider_raw
    assert provider_raw["default_endpoint"] == endpoint.endpoint_id
    assert provider_raw["endpoints"][endpoint.endpoint_id]["model_id"] == "glm-reasoning"
    assert "endpoint_id" not in provider_raw["endpoints"][endpoint.endpoint_id]
    assert provider_raw["endpoints"][endpoint.endpoint_id]["reasoning"] == reasoning.model_dump(
        mode="json"
    )

    with pytest.raises(ConfigError, match="reasoning_effort"):
        manager.update_endpoint(
            provider.provider_id,
            endpoint.endpoint_id,
            display_name=endpoint.display_name,
            request_options={"reasoning_effort": "low"},
        )


def test_provider_and_endpoint_mutations_translate_validation_errors(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)

    with pytest.raises(ConfigError, match="timeout_seconds"):
        manager.create_provider(
            display_name="GLM",
            provider_type="custom",
            protocol="openai-chat-completions",
            timeout_seconds=-1,
        )

    provider = manager.create_provider(
        display_name="GLM",
        provider_type="custom",
        protocol="openai-chat-completions",
    )
    with pytest.raises(ConfigError, match="model_id"):
        manager.create_endpoint(
            provider.provider_id,
            model_id="   ",
            base_url="https://example.test/v1",
        )

    with pytest.raises(ConfigError, match="could not serialize"):
        manager.create_endpoint(
            provider.provider_id,
            model_id="glm-chat",
            base_url="https://example.test/v1",
            request_options={"invalid": object()},
        )


def test_incomplete_provider_does_not_replace_safe_default(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)

    provider = manager.create_provider(
        display_name="GLM",
        provider_type="custom",
        protocol="openai-chat-completions",
    )

    assert provider.default_endpoint is None
    assert manager.snapshot().default_provider_id == "faux"
    with pytest.raises(ConfigError, match="default provider"):
        manager.update_provider(provider.provider_id, make_default=True)

    with pytest.raises(ConfigError, match="built-in Faux provider"):
        manager.create_provider(display_name="Faux", provider_type="faux")


def test_runtime_resolution_distinguishes_configured_and_ad_hoc_providers(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)

    with pytest.raises(ConfigError, match="provider not found"):
        manager.resolve_runtime_options(ProviderRuntimeOptions(provider_id="missing"))

    options = manager.resolve_runtime_options(
        ProviderRuntimeOptions(
            protocol="openai-chat-completions",
            model="ad-hoc-model",
            base_url="https://example.test/v1",
        )
    )

    assert options.protocol == "openai-chat-completions"
    assert options.provider_id is None
    assert options.endpoint_id is None


def test_raw_provider_timeout_zero_is_rejected_and_enabled_is_typed(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "providers.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "items": {
                        "glm": {
                            "display_name": "GLM",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
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
    provider = manager.create_provider(
        display_name="GLM",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.test/v1",
    )
    default_endpoint = manager.create_endpoint(
        provider.provider_id,
        model_id="glm-test",
        make_default=True,
    )
    with pytest.raises(ConfigError, match="system default endpoint is not configured"):
        manager.require_default_route()
    manager.set_default_provider(provider.provider_id)
    assert manager.snapshot().default_provider_id == provider.provider_id
    assert manager.require_default_route() == (
        f"{provider.provider_id}/{default_endpoint.endpoint_id}"
    )

    with pytest.raises(ConfigError, match="cannot remove the default endpoint"):
        manager.remove_endpoint(provider.provider_id, default_endpoint.endpoint_id)
    with pytest.raises(ConfigError, match="cannot disable the default provider"):
        manager.update_provider(provider.provider_id, enabled=False)
    with pytest.raises(ConfigError, match="cannot remove the default provider"):
        manager.remove_provider(provider.provider_id)
    assert manager.require_default_route() == (
        f"{provider.provider_id}/{default_endpoint.endpoint_id}"
    )


@pytest.mark.parametrize(
    "providers_section",
    [[], {"items": "invalid"}, {"items": 0}, {"default": 0, "items": {}}],
)
def test_invalid_provider_registry_shapes_fail_visibly(tmp_path, providers_section) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps({"providers": providers_section}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="providers"):
        ProviderConfigManager(tmp_path).snapshot()


def test_unknown_explicit_default_provider_fails_visibly(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps({"providers": {"default": "missing", "items": {}}}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="default provider not found"):
        ProviderConfigManager(tmp_path).snapshot()


def test_default_api_path_per_provider_protocol() -> None:
    assert default_api_path("openai-chat-completions") == "/chat/completions"
    assert default_api_path("faux") is None


def test_endpoint_defaults_match_mapping_defaults() -> None:
    endpoint = ProviderEndpointConfig(endpoint_id="chat", model_id="chat-model")
    mapped = endpoint_config_from_mapping("chat", {"model_id": "chat-model"})

    assert endpoint.context_window == mapped.context_window == 1_000_000
    assert endpoint.max_output_tokens == mapped.max_output_tokens == 32_768
    assert endpoint.supports_tools is True
    assert endpoint.supports_vision is False
    assert endpoint.supports_streaming is True
    assert endpoint.supports_structured_output is True
    assert mapped.supports_tools is True
    assert mapped.supports_vision is False
    assert mapped.supports_streaming is True
    assert mapped.supports_structured_output is True


def test_endpoint_api_path_is_optional_and_normalized() -> None:
    endpoint = ProviderEndpointConfig(endpoint_id="chat", model_id="chat-model")
    assert endpoint.api_path is None

    endpoint_with_blank = ProviderEndpointConfig(
        endpoint_id="chat", model_id="chat-model", api_path="  "
    )
    assert endpoint_with_blank.api_path is None

    endpoint_custom = ProviderEndpointConfig(
        endpoint_id="chat", model_id="chat-model", api_path="responses/chat"
    )
    assert endpoint_custom.api_path == "/responses/chat"


def test_provider_level_base_url_and_api_path_inherited_by_endpoint(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "glm",
                    "items": {
                        "glm": {
                            "display_name": "GLM",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
                            "credential_ref": "env:OURO_TEST_KEY",
                            "base_url": "https://provider.example.test/v1",
                            "api_path": "/custom/chat",
                            "default_endpoint": "chat",
                            "endpoints": {
                                "chat": {"model_id": "glm-chat"},
                            },
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

    assert options.base_url == "https://provider.example.test/v1"
    assert options.api_path == "/custom/chat"


def test_endpoint_explicit_base_url_and_api_path_override_provider_defaults(
    tmp_path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "glm",
                    "items": {
                        "glm": {
                            "display_name": "GLM",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
                            "credential_ref": "env:OURO_TEST_KEY",
                            "base_url": "https://provider.example.test/v1",
                            "api_path": "/custom/chat",
                            "default_endpoint": "chat",
                            "endpoints": {
                                "chat": {
                                    "model_id": "glm-chat",
                                    "base_url": "https://endpoint.example.test/v1",
                                    "api_path": "/endpoint/chat",
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
        ProviderRuntimeOptions(),
    )

    assert options.base_url == "https://endpoint.example.test/v1"
    assert options.api_path == "/endpoint/chat"


def test_endpoint_api_path_falls_back_to_protocol_default(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "glm",
                    "items": {
                        "glm": {
                            "display_name": "GLM",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
                            "credential_ref": "env:OURO_TEST_KEY",
                            "base_url": "https://example.test/v1",
                            "default_endpoint": "chat",
                            "endpoints": {
                                "chat": {"model_id": "glm-chat"},
                            },
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

    assert options.api_path == "/chat/completions"


def test_openai_chat_completions_endpoint_requires_base_url_at_provider_or_endpoint(
    tmp_path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "providers.json").write_text(
        json.dumps(
            {
                "providers": {
                    "default": "glm",
                    "items": {
                        "glm": {
                            "display_name": "GLM",
                            "provider_type": "custom",
                            "protocol": "openai-chat-completions",
                            "credential_ref": "env:OURO_TEST_KEY",
                            "default_endpoint": "chat",
                            "endpoints": {
                                "chat": {"model_id": "glm-chat"},
                            },
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="base_url is required"):
        ProviderConfigManager(tmp_path).snapshot()


def test_create_provider_with_base_url_but_no_model_keeps_empty_endpoints(
    tmp_path,
) -> None:
    manager = ProviderConfigManager(tmp_path)

    provider = manager.create_provider(
        display_name="GLM",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.test/v1",
        api_path="/responses/chat",
    )

    assert provider.endpoints == ()
    assert provider.base_url == "https://example.test/v1"
    assert provider.api_path == "/responses/chat"
    assert manager.snapshot().default_provider_id == "faux"

    # An endpoint can be added later and inherits the provider-level defaults.
    endpoint = manager.create_endpoint(
        provider.provider_id,
        model_id="glm-chat",
        make_default=True,
    )
    assert endpoint.base_url is None
    assert endpoint.api_path is None

    options = manager.resolve_runtime_options(
        ProviderRuntimeOptions(provider_id=provider.provider_id)
    )
    assert options.base_url == "https://example.test/v1"
    assert options.api_path == "/responses/chat"


def test_removing_default_endpoint_clears_provider_default(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    provider = manager.create_provider(
        display_name="GLM",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://chat.example.test/v1",
    )
    chat = manager.create_endpoint(
        provider.provider_id,
        model_id="glm-chat",
        make_default=True,
    )
    reasoning = manager.create_endpoint(
        provider.provider_id,
        model_id="glm-reasoning",
        base_url="https://reasoning.example.test/v1",
    )

    manager.remove_endpoint(provider.provider_id, chat.endpoint_id)
    restored = manager.get_provider(provider.provider_id)
    assert restored is not None

    assert restored.default_endpoint is None
    assert {endpoint.endpoint_id for endpoint in restored.endpoints} == {reasoning.endpoint_id}


def test_removing_system_default_endpoint_requires_a_new_selection(
    tmp_path,
) -> None:
    manager = ProviderConfigManager(tmp_path)
    provider = manager.create_provider(
        display_name="GLM",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://chat.example.test/v1",
    )
    endpoint = manager.create_endpoint(
        provider.provider_id,
        model_id="glm-chat",
        make_default=True,
    )
    manager.set_default_provider(provider.provider_id)
    assert manager.snapshot().default_provider_id == provider.provider_id

    with pytest.raises(ConfigError, match="select another default endpoint first"):
        manager.remove_endpoint(provider.provider_id, endpoint.endpoint_id)

    assert manager.require_default_route() == f"{provider.provider_id}/{endpoint.endpoint_id}"


def test_provider_retry_policy_round_trips_into_runtime_options(tmp_path) -> None:
    manager = ProviderConfigManager(tmp_path)
    policy = ProviderRetryPolicy(
        max_attempts=3,
        initial_backoff_seconds=0.25,
        max_backoff_seconds=2,
    )
    provider = manager.create_provider(
        display_name="GLM",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://chat.example.test/v1",
        retry_policy=policy,
    )
    endpoint = manager.create_endpoint(
        provider.provider_id,
        model_id="glm-chat",
        make_default=True,
    )

    restored = ProviderConfigManager(tmp_path).get_provider(provider.provider_id)
    options = manager.resolve_runtime_options(
        ProviderRuntimeOptions(
            provider_id=provider.provider_id,
            endpoint_id=endpoint.endpoint_id,
        )
    )

    assert restored is not None and restored.retry_policy == policy
    assert options.retry_policy == policy
    raw = json.loads((tmp_path / "config" / "providers.json").read_text(encoding="utf-8"))
    assert raw["providers"]["items"][provider.provider_id]["retry_policy"]["max_attempts"] == 3


def test_provider_retry_opt_out_survives_round_trip(tmp_path) -> None:
    # max_attempts=1 disables retries; it diverges from the default policy and
    # must be persisted, otherwise a reload would silently re-enable retries.
    manager = ProviderConfigManager(tmp_path)
    provider = manager.create_provider(
        display_name="GLM",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://chat.example.test/v1",
        retry_policy=ProviderRetryPolicy(max_attempts=1),
    )
    manager.create_endpoint(provider.provider_id, model_id="glm-chat", make_default=True)

    raw = json.loads((tmp_path / "config" / "providers.json").read_text(encoding="utf-8"))
    assert raw["providers"]["items"][provider.provider_id]["retry_policy"]["max_attempts"] == 1

    restored = ProviderConfigManager(tmp_path).get_provider(provider.provider_id)
    assert restored is not None and restored.retry_policy.max_attempts == 1


def test_invalid_provider_retry_policy_is_rejected_without_rewriting_config(tmp_path) -> None:
    config_path = tmp_path / "config" / "providers.json"
    config_path.parent.mkdir(parents=True)
    raw = {
        "providers": {
            "default": "faux",
            "items": {
                "glm": {
                    "display_name": "GLM",
                    "provider_type": "custom",
                    "protocol": "openai-chat-completions",
                    "base_url": "https://example.test/v1",
                    "retry_policy": {"max_attempts": 0},
                }
            },
        }
    }
    config_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="max_attempts"):
        ProviderConfigManager(tmp_path).snapshot()

    assert json.loads(config_path.read_text(encoding="utf-8")) == raw
