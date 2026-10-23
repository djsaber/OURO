import pytest

from ouro.application.configuration.facade import ConfigurationFacade
from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_models import ProviderConfig, ProviderRuntimeOptions
from ouro.config.provider_templates import provider_templates
from ouro.config.providers.runtime import merge_runtime_options
from ouro.core.errors import ConfigError
from ouro.providers.factory import build_runtime_adapter


@pytest.mark.parametrize(
    "protocol,encodable",
    [
        ("faux", set()),
        ("openai-chat-completions", {"image", "audio", "video"}),
        ("openai-responses", {"image"}),
        ("anthropic-messages", {"image"}),
    ],
)
@pytest.mark.parametrize("kind", ["image", "audio", "video"])
def test_protocol_encoding_limits_apply_to_every_declared_input_modality(protocol, encodable, kind):
    capability = {f"supports_{kind}_input": True}
    base_url = None if protocol == "faux" else "https://example.test"
    options = ProviderRuntimeOptions(
        protocol=protocol, model="media", base_url=base_url, **capability
    )
    config = dict(
        display_name="Media",
        provider_type="faux" if protocol == "faux" else "custom",
        protocol=protocol,
        base_url=base_url,
        endpoints=(ProviderEndpointConfig(model_id="media", **capability),),
    )
    if kind in encodable:
        ProviderConfig(**config)
        assert build_runtime_adapter(options).model.input_modalities == frozenset({kind})
    else:
        with pytest.raises(ValueError, match=f"does not support {kind} input"):
            ProviderConfig(**config)
        with pytest.raises(ConfigError, match=f"does not support {kind} input"):
            build_runtime_adapter(options)


def test_presets_only_enable_media_supported_by_the_configured_connection():
    audio_models = {
        "gemini-3.8-flash",
        "gemini-3.1-pro-preview",
        "gemini-3.5-flash-lite",
        "doubao-seed-2-1-lite-260915",
    }
    video_models = {
        "qwen3.8-max",
        "qwen3.8-flash",
        "qwen3.7-plus",
        "kimi-k3",
        "kimi-k2.7-code",
        "kimi-k2.7-code-highspeed",
        "kimi-k2.6",
        "MiniMax-M3",
        "doubao-seed-2-1-pro-260915",
        "doubao-seed-2-1-lite-260915",
    }
    text_only_models = {
        "deepseek-v4-pro",
        "glm-5.3",
        "glm-5.2",
        "glm-5-turbo",
        "MiniMax-M2.7",
        "MiniMax-M2.7-highspeed",
    }
    for template in provider_templates():
        if template.template_id.startswith("opencode-go"):
            # Go's gateway contracts are covered separately from direct-provider presets.
            continue
        for endpoint in template.endpoints:
            assert endpoint.supports_image_input == (endpoint.model_id not in text_only_models)
            assert endpoint.supports_audio_input == (endpoint.model_id in audio_models)
            assert endpoint.supports_video_input == (endpoint.model_id in video_models)


@pytest.mark.parametrize("protocol", ["openai-responses", "anthropic-messages"])
@pytest.mark.parametrize("capability", ["supports_audio_input", "supports_video_input"])
def test_incompatible_media_capability_is_rejected_before_config_write(
    tmp_path, protocol, capability
):
    config = ConfigurationFacade(tmp_path)
    provider = config.add_provider(
        display_name="Test",
        provider_type="custom",
        protocol=protocol,
        base_url="https://example.test",
    )
    provider_id = provider.changed_provider_id
    before = config.provider(provider_id)
    with pytest.raises(ConfigError, match="does not support"):
        config.add_endpoint(provider_id, model="test", **{capability: True})
    assert config.provider(provider_id) == before
    with pytest.raises(ConfigError, match="does not support"):
        build_runtime_adapter(
            ProviderRuntimeOptions(
                protocol=protocol,
                model="test",
                base_url="https://example.test",
                **{capability: True},
            )
        )


def test_media_capabilities_round_trip_through_configuration_and_runtime_overrides(tmp_path):
    config = ConfigurationFacade(tmp_path)
    provider_id = config.add_provider(
        display_name="Test",
        provider_type="custom",
        protocol="openai-chat-completions",
        base_url="https://example.test",
    ).changed_provider_id
    created = config.add_endpoint(
        provider_id, model="av", supports_audio_input=True, supports_video_input=True
    )
    saved = ConfigurationFacade(tmp_path).provider(provider_id).endpoints[0]
    assert (
        saved.supports_audio_input and saved.supports_video_input and not saved.supports_image_input
    )
    config.update_endpoint(
        provider_id, created.changed_endpoint_id, display_name=None, supports_audio_input=False
    )
    updated = ConfigurationFacade(tmp_path).provider(provider_id).endpoints[0]
    assert not updated.supports_audio_input and updated.supports_video_input
    options = merge_runtime_options(
        ProviderRuntimeOptions(
            protocol="openai-chat-completions",
            model="av",
            base_url="https://example.test",
            supports_audio_input=True,
            supports_video_input=True,
        ),
        ProviderRuntimeOptions(supports_video_input=False),
    )
    assert options.supports_audio_input and options.supports_video_input is False
    adapter = build_runtime_adapter(options)
    assert adapter.model.input_modalities == frozenset({"audio"})
