from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from ouro.core.base import new_id
from ouro.plugins.catalog.bindings import bind_generation_gate
from ouro.plugins.catalog.gates import GenerationGate
from ouro.plugins.contributions.binding import bind_execution_contract
from ouro.plugins.contributions.models import ContributionOwner, PreparedContributionSet
from ouro.plugins.contributions.preparation import ContributionCoordinator
from ouro.plugins.inprocess.contributions import adapt_sdk_contributions
from ouro.plugins.invocation import current_invocation
from ouro.plugins.manifest import PluginManifest, ToolContributionSpec
from ouro.plugins.models import PluginGenerationRecord
from ouro.sdk.plugins import v5 as sdk
from ouro.tools import Tool


@dataclass(frozen=True)
class ToolGroupPublication:
    contribution: sdk.ToolGroupContribution
    registration_id: str = field(default_factory=lambda: new_id("tgp"))
    on_release: Callable[[], None] | None = None


class ToolGroupVersion:
    """Retain an immutable publication until its accepted decisions release it."""

    def __init__(
        self,
        publication: ToolGroupPublication,
        record: PluginGenerationRecord,
        contributions: PreparedContributionSet,
    ) -> None:
        self.group_id = publication.contribution.id
        self.registration_id = publication.registration_id
        self._on_release = publication.on_release
        self.record = record
        self.contributions = contributions
        self.gate = GenerationGate(f"{record.generation_id}/tool_groups/{self.registration_id}")
        self._leases = 0
        self._retired = False
        self._lock = threading.Lock()

    def acquire(self) -> ToolGroupLease:
        with self._lock:
            if not self.gate.is_open:
                raise RuntimeError(f"tool group publication is unavailable: {self.group_id}")
            self._leases += 1
        return ToolGroupLease(self)

    def retire(self) -> None:
        with self._lock:
            self._retired = True
            self._close_if_unused()

    def release(self) -> None:
        with self._lock:
            self._leases -= 1
            self._close_if_unused()

    def _close_if_unused(self) -> None:
        if self._retired and self._leases == 0:
            self.gate.close()
            release, self._on_release = self._on_release, None
            if release is not None:
                release()


class ToolGroupLease:
    def __init__(self, version: ToolGroupVersion) -> None:
        self._version = version
        self._closed = False

    @property
    def record(self) -> PluginGenerationRecord:
        return self._version.record

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._version.release()


class ToolGroupRegistration:
    def __init__(self, endpoint: PluginToolGroups, version: ToolGroupVersion) -> None:
        self._endpoint: PluginToolGroups | None = endpoint
        self._group_id = version.group_id
        self._registration_id = version.registration_id

    async def close(self) -> None:
        if self._endpoint is None:
            return
        await self._endpoint.withdraw(self._group_id, self._registration_id)
        self._endpoint = None


type PublishToolGroup = Callable[
    ["PluginToolGroups", str, ToolGroupVersion | None, ToolGroupVersion | None], Awaitable[None]
]


class PluginToolGroups:
    """Validate a generation's tool groups and request Host catalog publication."""

    def __init__(
        self,
        manifest: PluginManifest,
        record: PluginGenerationRecord,
        owner: ContributionOwner,
        gate: GenerationGate,
        publish: PublishToolGroup,
    ) -> None:
        self.manifest = manifest
        self.record = record
        self.owner = owner
        self.gate = gate
        self._publish = publish
        self._groups: dict[str, ToolGroupVersion] = {}

    @property
    def versions(self) -> tuple[ToolGroupVersion, ...]:
        return tuple(self._groups.values())

    def get(self, group_id: str) -> ToolGroupVersion | None:
        return self._groups.get(group_id)

    def prepare(self, groups: Sequence[ToolGroupPublication]) -> None:
        if len({group.contribution.id for group in groups}) != len(groups):
            raise ValueError("duplicate initial tool group id")
        self._groups = {group.contribution.id: self._prepare(group) for group in groups}

    def require_writable(self) -> None:
        invocation = current_invocation.get()
        if invocation is not None:
            invocation.require_active()
            if not invocation.writable:
                raise PermissionError("read-only plugin callbacks cannot publish tool groups")
        if not self.gate.is_open:
            raise RuntimeError("tool groups require an active plugin generation")

    async def publish(self, group: sdk.ToolGroupContribution) -> ToolGroupRegistration:
        return await self.publish_owned(ToolGroupPublication(group))

    async def publish_owned(self, publication: ToolGroupPublication) -> ToolGroupRegistration:
        self.require_writable()
        version = self._prepare(publication)
        await self._publish(self, version.group_id, version, None)
        return ToolGroupRegistration(self, version)

    async def withdraw(self, group_id: str, registration_id: str) -> None:
        # Revocation already removes this owner from executable catalogs. Stop
        # callbacks may still close their handles while Host cleanup drains it.
        version = self.get(group_id)
        if not self.gate.is_open or version is None or version.registration_id != registration_id:
            return
        self.require_writable()
        await self._publish(self, version.group_id, None, version)

    def commit(self, group_id: str, version: ToolGroupVersion | None) -> None:
        previous = self._groups.pop(group_id, None)
        if version is not None:
            self._groups[group_id] = version
        if previous is not None:
            previous.retire()

    async def close(self) -> None:
        for version in self._groups.values():
            version.retire()
        self._groups.clear()

    def _prepare(self, publication: ToolGroupPublication) -> ToolGroupVersion:
        group = publication.contribution
        declaration = next(
            (item for item in self.manifest.tool_groups if item.id == group.id), None
        )
        if declaration is None:
            raise ValueError(f"tool group is not declared: {group.id}")
        if not group.revision.strip() or group.revision != group.revision.strip():
            raise ValueError("tool group revision must be nonempty with no surrounding whitespace")
        if len({tool.id for tool in group.tools}) != len(group.tools):
            raise ValueError(f"duplicate tool id in group: {group.id}")
        declarations = [
            ToolContributionSpec(
                **declaration.model_dump(exclude={"id"}),
                id=tool.id,
                exposed_name=tool.exposed_name,
                description=tool.description,
            )
            for tool in group.tools
        ]
        adapted = adapt_sdk_contributions(
            sdk.ContributionSet(tools=list(group.tools)), owner_id=self.manifest.plugin_id
        )
        tools = ContributionCoordinator.prepare_tools(
            self.manifest,
            self.owner,
            "official" if self.owner.plugin_class == "builtin" else "third_party",
            adapted.tools,
            declarations=declarations,
        )
        bound: list[Tool] = []
        hashes: dict[str, str] = {}
        for tool in tools:
            metadata = dict(tool.definition.metadata)
            identity = f"{group.id}/{metadata['contribution_id']}"
            contract_hash = hashlib.sha256(
                json.dumps(
                    [identity, group.revision, metadata["contract_hash"]], separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            metadata.update(contribution_id=identity, contract_hash=contract_hash)
            bound.append(
                Tool(
                    definition=tool.definition.model_copy(update={"metadata": metadata}),
                    handler=tool.handler,
                    resume_handler=tool.resume_handler,
                )
            )
            hashes[f"{self.owner.plugin_id}/tools/{identity}"] = contract_hash
        prepared = PreparedContributionSet(self.owner, tools=tuple(bound), contract_hashes=hashes)
        execution_hash = self.record.execution_contract_hash
        assert execution_hash is not None
        prepared = bind_execution_contract(prepared, execution_hash)
        prepared = bind_generation_gate(prepared, self.gate)
        return ToolGroupVersion(publication, self.record, prepared)
