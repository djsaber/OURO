"""Convert owned configuration structures without starting application services."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from ouro.config.agents import AgentDefinitionsConfig
from ouro.config.endpoints import ProviderEndpointConfig
from ouro.config.provider_models import BUILTIN_FAUX_PROVIDER_ID
from ouro.config.providers.codec import provider_config_from_mapping
from ouro.config.providers.template_store import template_fingerprint
from ouro.config.reasoning import ReasoningProfileConfig
from ouro.core.errors import ConfigError
from ouro_upgrade.agents import upgrade_definition, upgrade_spawn_policy
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


def _reasoning_controls(endpoint: dict[str, Any]) -> None:
    if "reasoning" not in endpoint:
        return
    reasoning = _object(endpoint["reasoning"])
    capability = _object(reasoning.get("capability", {}))
    profile = reasoning.get("profile")
    if profile is not None:
        profile = _object(profile)
    historical = "default_effort" in capability or (profile is not None and "variants" in profile)
    if historical:
        default = _object(capability.get("default", {}))
        require(
            "supported_summaries" not in capability
            and "summary" not in default
            and (profile is None or not {"effort", "summary"}.intersection(profile)),
            _PATH,
            "mixed historical and current reasoning fields",
        )
        enabled_effort = capability.pop("default_effort", None)
        selected_effort = default.get("effort")
        require(
            enabled_effort is None or selected_effort is None or enabled_effort == selected_effort,
            _PATH,
            "distinct historical default efforts cannot be represented by one default",
        )
        require(
            default.get("mode") != "disabled" or selected_effort is None,
            _PATH,
            "invalid historical disabled reasoning preference",
        )
        if selected_effort is not None and default.get("mode", "default") == "default":
            default["mode"] = "enabled"
        if selected_effort is None and enabled_effort is not None:
            default["effort"] = enabled_effort
        if default or "default" in capability:
            capability["default"] = default
        if profile is not None and "variants" in profile:
            profile["effort"] = profile.pop("variants")
            profile["summary"] = {}
        capability["supported_summaries"] = []
        reasoning["capability"] = capability
    capability.pop("output", None)
    capability.pop("continuation", None)


def _reasoning_text_field(endpoint: dict[str, Any]) -> None:
    reasoning = endpoint.get("reasoning", {})
    profile = reasoning.get("profile")
    if profile is None or "text_field" not in profile:
        return
    require(
        "reasoning_text_field" not in endpoint,
        _PATH,
        "mixed historical and current reasoning text fields",
    )
    field = profile.pop("text_field")
    require(
        field in (None, "reasoning_content", "reasoning"), _PATH, "invalid reasoning text field"
    )
    if field is not None:
        endpoint["reasoning_text_field"] = field
        if not profile.get("effort") and not profile.get("summary"):
            # Validate before retiring the profile so malformed controls are not discarded.
            ReasoningProfileConfig.model_validate(profile)
            reasoning["profile"] = None


# Unversioned formats use ordered, explicitly recognized structural transitions.
ENDPOINT_STEPS: tuple[Callable[[dict[str, Any]], None], ...] = (
    _vision_capabilities,
    _reasoning_controls,
    _reasoning_text_field,
)


def _previous_endpoint_fingerprint(
    endpoint: ProviderEndpointConfig, source: dict[str, Any], template_endpoint_id: str
) -> str:
    """Reconstruct the source schema's normalized hash without inferring template content."""
    business = endpoint.model_dump(mode="json")
    business["endpoint_id"] = template_endpoint_id
    previous = source.get("reasoning") or {}
    source_capability = previous.get("capability") or {}
    capability = business["reasoning"]["capability"]
    profile = previous.get("profile")
    historical = "supported_summaries" not in source_capability
    for field in ("output", "continuation"):
        if historical or field in source_capability:
            capability[field] = source_capability.get(field, "none")
    if historical:
        capability.pop("supported_summaries")
        default = source_capability.get("default") or {}
        capability["default"] = {
            "mode": default.get("mode", "default"),
            "effort": default.get("effort"),
        }
        effort = source_capability.get("default_effort") or default.get("effort")
        if effort is None and len(capability["supported_efforts"]) == 1:
            effort = capability["supported_efforts"][0]
        if effort is not None:
            capability["default_effort"] = effort
        if profile is not None and "variants" in profile:
            business["reasoning"]["profile"] = {
                "name": profile["name"].strip(),
                "variants": profile["variants"],
            }
    if profile is not None and "text_field" in profile:
        business.pop("reasoning_text_field", None)
        if business["reasoning"]["profile"] is None:
            business["reasoning"]["profile"] = {
                "name": profile["name"].strip(),
                "effort": {},
                "summary": {},
            }
        if profile["text_field"] is not None:
            business["reasoning"]["profile"]["text_field"] = profile["text_field"]
    return template_fingerprint(business)


def _rebase_endpoint_bindings(
    provider: dict[str, Any], source: dict[str, Any], endpoints: tuple[ProviderEndpointConfig, ...]
) -> None:
    binding = provider.get("template")
    if not binding:
        return
    by_id = {endpoint.endpoint_id: endpoint for endpoint in endpoints}
    for item in binding.get("endpoints", []):
        endpoint_id = item["configured_endpoint_id"]
        endpoint = by_id.get(endpoint_id)
        if endpoint is None:
            continue
        template_id = item["template_endpoint_id"]
        original = source["endpoints"][endpoint_id]
        if item["fingerprint"] == _previous_endpoint_fingerprint(endpoint, original, template_id):
            business = endpoint.model_dump(mode="json")
            business["endpoint_id"] = template_id
            item["fingerprint"] = template_fingerprint(business)


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
        try:
            for raw_endpoint in _object(provider.get("endpoints", {})).values():
                endpoint = _object(raw_endpoint)
                for step in ENDPOINT_STEPS:
                    step(endpoint)
            parsed = provider_config_from_mapping(provider_id, provider)
        except UpgradeDataError:
            raise
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
        _rebase_endpoint_bindings(
            provider, raw["providers"]["items"][provider_id], parsed.endpoints
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


def convert_agents(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate source contracts before retiring inert dispatch settings."""
    path = "config/agents.json"
    value = copy.deepcopy(raw)
    section = value.get("agents")
    if section is None:
        return value
    require(isinstance(section, dict), path, "expected agents object")
    try:
        for definition in section.get("definitions", []):
            upgrade_definition(definition)
        runtime = section.get("runtime", {})
        if "max_depth" in runtime:
            require(
                type(runtime["max_depth"]) is int and runtime["max_depth"] == 1,
                path,
                "unsupported agent depth",
            )
            runtime.pop("max_depth")
        policy = runtime.get("spawn_policy", {})
        upgrade_spawn_policy(policy)
        AgentDefinitionsConfig.model_validate(section)
    except UpgradeDataError:
        raise
    except ValueError, TypeError, AttributeError:
        raise UpgradeDataError(f"{path}: invalid agent configuration") from None
    return value


def agents(work: Workspace) -> None:
    path = "config/agents.json"
    if not work.path(path).exists():
        return
    raw = read_json(work, path)
    converted = convert_agents(raw)
    if converted != raw:
        work.json(path, converted)
