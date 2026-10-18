from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextvars import Context
from typing import Any, Protocol

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.plugins.manifest import ToolContributionSpec
from ouro.plugins.tool_groups import PluginToolGroups, ToolGroupPublication
from ouro.plugins.worker_services import WorkerRequests
from ouro.sdk.plugins import v5 as sdk


class ToolGroupWorker(WorkerRequests, Protocol):
    async def notify(self, method: str, params: Mapping[str, object] | None = None) -> None: ...


class WorkerToolDescription(ToolContributionSpec):
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    result_schema: dict[str, Any] = Field(default_factory=dict)


class WorkerToolGroupDescription(OuroModel):
    id: str
    revision: str
    registration_id: str = Field(min_length=1)
    tools: list[WorkerToolDescription]


class WorkerToolGroupBridge:
    """Bind wire publication identities to Host contracts and lease-driven release."""

    def __init__(
        self,
        client: ToolGroupWorker,
        endpoint: PluginToolGroups,
        handler: Callable[[str, str], sdk.ToolHandler],
        resume_handler: Callable[[str, str], sdk.ToolResumeHandler],
        diagnostic: Callable[[str, str], None],
    ) -> None:
        self.client = client
        self.endpoint = endpoint
        self._handler = handler
        self._resume_handler = resume_handler
        self._diagnostic = diagnostic
        self._owned: dict[str, str] = {}
        self._cleanup: set[asyncio.Task[None]] = set()
        self._loop = asyncio.get_running_loop()
        self._closed = False

    def prepare(self, raw: object) -> list[ToolGroupPublication]:
        if not isinstance(raw, dict):
            raise ValueError("worker tool_groups must contain group descriptors")
        publications = []
        for group_id, descriptor in raw.items():
            publication = self._decode(descriptor)
            if publication.contribution.id != group_id:
                raise ValueError("worker tool group id must match its descriptor key")
            self._owned[publication.registration_id] = group_id
            publications.append(publication)
        return publications

    async def dispatch(self, method: str, params: Mapping[str, object]) -> object:
        if method == "tool_groups.publish":
            publication = self._decode(params)
            await self.endpoint.publish_owned(publication)
            self._owned[publication.registration_id] = publication.contribution.id
            return None
        if method == "tool_groups.close":
            identity = params.get("registration_id")
            if not isinstance(identity, str) or not identity:
                raise ValueError("tool group registration_id must be a nonempty string")
            group_id = self._owned.get(identity)
            if group_id is not None:
                await self.endpoint.withdraw(group_id, identity)
            return identity in self._owned
        raise ValueError(f"unknown Host tool group method: {method}")

    def _decode(self, raw: object) -> ToolGroupPublication:
        value = WorkerToolGroupDescription.model_validate(raw)
        identity = value.registration_id
        if identity in self._owned:
            raise ValueError("tool group registration identity is already in use")
        tools = tuple(
            sdk.ToolContribution(
                **tool.model_dump(exclude={"resumable", "optional"}),
                handler=self._handler(tool.id, identity),
                resume_handler=self._resume_handler(tool.id, identity) if tool.resumable else None,
            )
            for tool in value.tools
        )
        return ToolGroupPublication(
            sdk.ToolGroupContribution(value.id, value.revision, tools),
            registration_id=identity,
            on_release=lambda: self._schedule_release(identity),
        )

    def _schedule_release(self, identity: str) -> None:
        if not self._closed:
            self._loop.call_soon_threadsafe(self._start_release, identity, context=Context())

    def _start_release(self, identity: str) -> None:
        if self._closed:
            return
        task = asyncio.create_task(self._release(identity), name="ouro-worker-tool-group-release")
        self._cleanup.add(task)
        task.add_done_callback(self._cleanup.discard)

    async def _release(self, identity: str) -> None:
        try:
            if self.client.is_healthy:
                await self.client.notify("tool_groups.release", {"registration_id": identity})
        except Exception as exc:
            if self.client.is_healthy:
                self._diagnostic("worker_tool_group_release_failed", f"{type(exc).__name__}: {exc}")
                return
        self._owned.pop(identity, None)

    async def close(self) -> None:
        # The process must already be stopped; all remote handlers are gone.
        self._closed = True
        tasks = tuple(self._cleanup)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._cleanup.clear()
        self._owned.clear()
