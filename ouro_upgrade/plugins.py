"""Upgrade plugin installation metadata without loading packages or private data."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import ValidationError

from ouro_upgrade.errors import UpgradeDataError, require
from ouro_upgrade.files import read_json
from ouro_upgrade.workspace import Workspace

PATH = "plugins/state.json"


def _discard_copy(value: dict[str, Any], key: str, expected: Any) -> None:
    if key in value:
        require(value[key] == expected, PATH, f"inconsistent {key}")
        value.pop(key)


def convert_state(raw: dict[str, Any]) -> dict[str, Any]:
    # Plugin schemas are needed only for homes with installation metadata.
    from ouro.plugins.models import PluginStateDocument

    value = copy.deepcopy(raw)
    try:
        for record in value.get("plugins", {}).values():
            for installed in record["installs"]:
                _discard_copy(installed, "plugin_id", installed["manifest"]["plugin_id"])
                _discard_copy(installed, "version", installed["manifest"]["version"])
                _discard_copy(
                    installed["provenance"], "package_digest", installed["package_digest"]
                )
            activation = record["activation"]
            _discard_copy(
                activation,
                "active_configuration_revision_id",
                activation["configuration_revision_by_digest"].get(activation["selected_digest"]),
            )
        PluginStateDocument.model_validate(value)
    except UpgradeDataError:
        raise
    except ValidationError, KeyError, TypeError, AttributeError:
        raise UpgradeDataError(f"{PATH}: invalid plugin installation state") from None
    return value


def state(work: Workspace) -> None:
    if work.path(PATH).exists():
        raw = read_json(work, PATH)
        converted = convert_state(raw)
        if converted != raw:
            work.json(PATH, converted)
