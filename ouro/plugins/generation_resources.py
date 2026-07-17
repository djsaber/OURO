from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ouro.plugins.mcp import PluginMCPResource
from ouro.plugins.models import (
    InstalledPlugin,
    PluginConfigurationRevision,
    PluginGenerationRecord,
)
from ouro.plugins.package import resolve_manifest_path
from ouro.skills import SkillLoader, SkillMetadata
from ouro.tools import Tool


@dataclass(frozen=True)
class GenerationResourceRequest:
    installed: InstalledPlugin
    generation: PluginGenerationRecord
    package_root: Path
    configuration: PluginConfigurationRevision
    credential_resolver: Callable[[str], str | None] | None = None


@dataclass(frozen=True)
class PreparedGenerationResources:
    skills: tuple[SkillMetadata, ...] = ()
    discovered_tools: tuple[Tool, ...] = ()
    owned_resources: tuple[Any, ...] = ()
    degraded_reasons: tuple[str, ...] = ()
    resource_contracts: dict[str, object] = field(default_factory=dict)

    def merge(self, other: PreparedGenerationResources) -> PreparedGenerationResources:
        duplicate_contracts = self.resource_contracts.keys() & other.resource_contracts.keys()
        if duplicate_contracts:
            identities = ", ".join(sorted(duplicate_contracts))
            raise ValueError(f"duplicate generation resource contract identity: {identities}")
        return PreparedGenerationResources(
            skills=(*self.skills, *other.skills),
            discovered_tools=(*self.discovered_tools, *other.discovered_tools),
            owned_resources=(*self.owned_resources, *other.owned_resources),
            degraded_reasons=(*self.degraded_reasons, *other.degraded_reasons),
            resource_contracts={
                **self.resource_contracts,
                **other.resource_contracts,
            },
        )


class GenerationResourceAdapter(Protocol):
    @property
    def kind(self) -> str: ...

    async def prepare(
        self,
        request: GenerationResourceRequest,
    ) -> PreparedGenerationResources: ...


@dataclass(frozen=True)
class PluginSkillResourceAdapter:
    kind: str = "skills"

    async def prepare(
        self,
        request: GenerationResourceRequest,
    ) -> PreparedGenerationResources:
        skills: list[SkillMetadata] = []
        degraded_reasons: list[str] = []
        contracts: dict[str, object] = {}
        for declaration in request.installed.manifest.contributes.skills:
            declared_path = resolve_manifest_path(
                request.package_root,
                declaration.path,
                allow_directory=True,
            )
            root = declared_path.parent if declared_path.is_file() else declared_path
            catalog = SkillLoader(root, default_source="plugin").load_catalog()
            if not catalog.skills:
                if declaration.optional:
                    continue
                detail = "; ".join(item.message for item in catalog.diagnostics)
                suffix = f": {detail}" if detail else ""
                raise ValueError(f"plugin skill contribution is empty: {declaration.id}{suffix}")
            if len(catalog.skills) != 1:
                raise ValueError(
                    f"plugin skill contribution must resolve to exactly one skill: {declaration.id}"
                )
            skill = catalog.skills[0]
            if skill.name != declaration.id:
                raise ValueError(
                    "plugin skill name does not match contribution id: "
                    f"{skill.name} != {declaration.id}"
                )
            skills.append(
                skill.model_copy(
                    update={
                        "extensions": {
                            **skill.extensions,
                            "ouro": {
                                "plugin_id": request.installed.plugin_id,
                                "package_digest": request.installed.package_digest,
                                "generation_id": request.generation.generation_id,
                                "contribution_id": declaration.id,
                            },
                        }
                    }
                )
            )
            degraded_reasons.extend(
                f"skills/{declaration.id}: {diagnostic.message}"
                for diagnostic in catalog.diagnostics
            )
            contracts[f"{request.installed.plugin_id}/skills/{declaration.id}"] = {
                "path": declaration.path
            }
        return PreparedGenerationResources(
            skills=tuple(skills),
            degraded_reasons=tuple(degraded_reasons),
            resource_contracts=contracts,
        )


@dataclass(frozen=True)
class PluginMCPResourceAdapter:
    kind: str = "mcp_servers"

    async def prepare(
        self,
        request: GenerationResourceRequest,
    ) -> PreparedGenerationResources:
        if not request.installed.manifest.contributes.mcp_servers:
            return PreparedGenerationResources()
        resource = PluginMCPResource(
            request.installed.manifest,
            request.package_root,
            resolve_credential=request.credential_resolver,
            credential_bindings=request.configuration.credential_refs,
        )
        try:
            tools = await resource.start()
        except BaseException:
            with suppress(Exception):
                await resource.close()
            raise
        return PreparedGenerationResources(
            discovered_tools=tools,
            owned_resources=(resource,),
            degraded_reasons=tuple(resource.degraded_reasons),
            resource_contracts={
                f"{request.installed.plugin_id}/mcp_servers/{declaration.id}": {
                    "path": declaration.path,
                    "optional": declaration.optional,
                }
                for declaration in request.installed.manifest.contributes.mcp_servers
                if declaration.id in resource.active_server_ids
            },
        )


SKILL_RESOURCE_ADAPTER = PluginSkillResourceAdapter()
MCP_RESOURCE_ADAPTER = PluginMCPResourceAdapter()


async def prepare_generation_resources(
    request: GenerationResourceRequest,
) -> PreparedGenerationResources:
    from ouro.plugins.contribution_types import contribution_type_adapters

    prepared = PreparedGenerationResources()
    try:
        for contribution_type in contribution_type_adapters():
            adapter = contribution_type.resource
            if adapter is None:
                continue
            prepared = prepared.merge(await adapter.prepare(request))
    except BaseException:
        for resource in reversed(prepared.owned_resources):
            with suppress(Exception):
                await resource.close()
        raise
    return prepared


__all__ = [
    "GenerationResourceAdapter",
    "GenerationResourceRequest",
    "PreparedGenerationResources",
    "prepare_generation_resources",
]
