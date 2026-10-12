from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from ouro.core.base import OuroModel


class PluginImplementation(OuroModel):
    """Immutable identity of one participant in accepted plugin work."""

    model_config = ConfigDict(frozen=True)

    plugin_id: str = Field(min_length=1)
    plugin_class: Literal["user", "builtin"]
    release_id: str = Field(min_length=1)
    configuration_revision_id: str = Field(min_length=1)
    execution_contract_hash: str = Field(min_length=1)
