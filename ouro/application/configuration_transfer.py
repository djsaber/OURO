from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import ValidationError

from ouro.config.agents import AgentDefinitionsConfigManager
from ouro.config.compaction import CompactionConfigManager
from ouro.config.file import JsonConfigFile
from ouro.config.prompts import PromptConfigManager
from ouro.config.provider_template_store import (
    SCHEMA_VERSION,
    ProviderTemplateStore,
)
from ouro.config.provider_templates import ProviderTemplate
from ouro.config.providers import ProviderConfigManager
from ouro.core.errors import ConfigError
from ouro.mcp.configuration import MCPConfigStore
from ouro.transfer.archive import ArchiveManifest, DataArchive, DataKind, DataTransferError

CONFIG_KINDS = frozenset({"providers", "provider-templates", "mcp", "agents", "prompts"})


def export_configuration(home: Path, kind: DataKind) -> DataArchive:
    if kind == "provider-templates":
        ProviderTemplateStore(home).catalog()
    raw = JsonConfigFile(home, name=f"{kind}.json").read()
    if kind == "provider-templates" and not raw:
        raise DataTransferError("provider template catalog could not be loaded")
    validate_configuration(kind, raw)
    return DataArchive(ArchiveManifest(kind=kind, data=raw))


def import_configuration(home: Path, archive: DataArchive, *, replace: bool) -> int:
    kind = archive.manifest.kind
    if archive.files:
        raise DataTransferError("configuration archives cannot contain data files")
    raw = archive.manifest.data
    validate_configuration(kind, raw)
    target = JsonConfigFile(home, name=f"{kind}.json")
    with target.mutation():
        existing = target.read()
        if existing and not replace:
            raise DataTransferError(f"{kind} configuration exists; use --replace to replace it")
        if kind == "providers":
            _validate_compaction_reference(home, raw)
        if kind in {"agents", "prompts"}:
            # A replacement is a new local revision, never an imported CAS token.
            revision = (
                AgentDefinitionsConfigManager(home).snapshot().revision
                if kind == "agents"
                else PromptConfigManager(home).snapshot().config.revision
            )
            raw = {kind: {**(raw.get(kind) or {}), "revision": revision + 1}}
        target.write(raw)
    return 1


def validate_configuration(kind: DataKind, raw: dict[str, Any]) -> None:
    try:
        if kind == "provider-templates":
            _validate_templates(raw)
            return
        if set(raw) - {kind}:
            raise DataTransferError(f"unexpected root fields in {kind} configuration")
        with TemporaryDirectory(prefix="ouro-config-check-") as directory:
            JsonConfigFile(directory, name=f"{kind}.json").write(raw)
            if kind == "providers":
                ProviderConfigManager(directory).snapshot(include_builtin=False)
            elif kind == "mcp":
                MCPConfigStore(directory).snapshot()
            elif kind == "agents":
                AgentDefinitionsConfigManager(directory).snapshot()
            elif kind == "prompts":
                PromptConfigManager(directory).snapshot()
            else:
                raise DataTransferError(f"unsupported configuration kind: {kind}")
    except (ConfigError, ValidationError, TypeError, ValueError) as exc:
        # Configuration validators may include input values. Keep foreign values
        # out of host error output, including malformed secret-bearing fields.
        raise DataTransferError(f"invalid {kind} configuration in archive") from exc


def _validate_templates(raw: dict[str, Any]) -> None:
    if (
        set(raw) != {"schema_version", "templates"}
        or type(raw["schema_version"]) is not int
        or raw["schema_version"] != SCHEMA_VERSION
    ):
        raise DataTransferError("unsupported provider template catalog schema")
    templates = raw["templates"]
    if not isinstance(templates, dict):
        raise DataTransferError("provider templates must be an object")
    for key, value in templates.items():
        if not isinstance(value, dict):
            raise DataTransferError("invalid provider template entry")
        template = ProviderTemplate.model_validate(value)
        if key != template.template_id:
            raise DataTransferError("provider template identity does not match its key")


def _validate_compaction_reference(home: Path, raw: dict[str, Any]) -> None:
    model = CompactionConfigManager(home).snapshot().model
    if not model.has_dedicated_endpoint:
        return
    with TemporaryDirectory(prefix="ouro-route-check-") as directory:
        JsonConfigFile(directory, name="providers.json").write(raw)
        provider = ProviderConfigManager(directory).get_provider(model.provider_id or "")
        if provider is None or provider.endpoint(model.endpoint_id) is None:
            raise DataTransferError("import would invalidate the configured compaction model")
