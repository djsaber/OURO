"""Prepare declared skills and MCP resources for activation."""

from __future__ import annotations

from ouro.plugins.lifecycle.generation_resources import (
    GenerationResourceRequest,
    PreparedGenerationResources,
)


async def prepare_generation_resources(
    request: GenerationResourceRequest,
) -> PreparedGenerationResources:
    from ouro.plugins.contributions.inventory import contribution_type_adapters

    prepared = PreparedGenerationResources()
    for contribution_type in contribution_type_adapters():
        adapter = contribution_type.resource
        if adapter is None:
            continue
        prepared = prepared.merge(await adapter.prepare(request))
    return prepared
