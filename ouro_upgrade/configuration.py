"""Convert known persisted provider configuration without resolving credentials."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from ouro.config.provider_codec import provider_config_from_mapping
from ouro.config.provider_models import BUILTIN_FAUX_PROVIDER_ID
from ouro.core.errors import ConfigError
from ouro_upgrade.errors import UpgradeDataError, require
from ouro_upgrade.files import read_json
from ouro_upgrade.workspace import Workspace

_PATH = "config/providers.json"
_MEDIA_FIELDS = ("supports_image_input", "supports_audio_input", "supports_video_input")


def _object(value: Any) -> dict[str, Any]:
    require(isinstance(value, dict), _PATH, "expected configuration object")
    return value


def _vision_capabilities(endpoint: dict[str, Any]) -> None:
    if "supports_vision" not in endpoint:
        return
    require(type(endpoint["supports_vision"]) is bool, _PATH, "invalid vision capability")
    require(
        not any(field in endpoint for field in _MEDIA_FIELDS),
        _PATH,
        "mixed historical and current capability fields",
    )
    endpoint["supports_image_input"] = endpoint.pop("supports_vision")
    endpoint["supports_audio_input"] = False
    endpoint["supports_video_input"] = False


# Unversioned formats use ordered, explicitly recognized structural transitions.
ENDPOINT_STEPS: tuple[Callable[[dict[str, Any]], None], ...] = (_vision_capabilities,)


def convert_providers(raw: dict[str, Any]) -> dict[str, Any]:
    """Preserve raw values; codecs validate the result but do not rewrite it."""
    require(set(raw) <= {"providers"}, _PATH, "unsupported root configuration fields")
    value = copy.deepcopy(raw)
    if value.get("providers") is None:
        return value
    section = _object(value["providers"])
    require(set(section) <= {"default", "items"}, _PATH, "unsupported provider registry fields")
    items = _object(section.get("items", {}))
    for provider_id, raw_provider in items.items():
        provider = _object(raw_provider)
        for raw_endpoint in _object(provider.get("endpoints", {})).values():
            endpoint = _object(raw_endpoint)
            for step in ENDPOINT_STEPS:
                step(endpoint)
        try:
            parsed = provider_config_from_mapping(provider_id, provider)
        except ConfigError, TypeError, ValueError:
            # Codec validation errors can include inline credentials or header values.
            raise UpgradeDataError(f"{_PATH}: invalid provider configuration") from None
        require(
            parsed.provider_id != BUILTIN_FAUX_PROVIDER_ID
            and parsed.provider_type != "faux"
            and parsed.protocol != "faux",
            _PATH,
            "reserved provider configuration",
        )
    if "default" in section:
        default = section["default"]
        require(
            isinstance(default, str)
            and bool(default.strip())
            and (default.strip() == BUILTIN_FAUX_PROVIDER_ID or default.strip() in items),
            _PATH,
            "invalid default provider reference",
        )
    return value


def providers(work: Workspace) -> None:
    if not work.path(_PATH).exists():
        return
    raw = read_json(work, _PATH)
    converted = convert_providers(raw)
    if converted != raw:
        work.json(_PATH, converted)
