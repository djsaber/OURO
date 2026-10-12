"""Publish SDK tool groups and retain their handlers until the Host releases them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields
from typing import Any
from uuid import uuid4

from .v3 import ToolContribution, ToolGroupContribution
from .worker import WorkerPeer
from .worker_tools import invoke_worker_tool


class WorkerToolGroupRegistration:
    def __init__(self, owner: WorkerToolGroups, identity: str) -> None:
        self._owner: WorkerToolGroups | None = owner
        self._identity = identity

    async def close(self) -> None:
        if self._owner is not None:
            await self._owner.withdraw(self._identity)
            self._owner = None


class WorkerToolGroups:
    """Install the tool_groups call, resume and release routes on one WorkerPeer."""

    def __init__(self, peer: WorkerPeer) -> None:
        self.peer = peer
        self._handlers: dict[str, dict[str, ToolContribution]] = {}
        peer.handlers["tool_groups.call"] = self.call
        peer.handlers["tool_groups.resume"] = self.resume
        peer.notifications["tool_groups.release"] = self.release

    def prepare(self, groups: Sequence[ToolGroupContribution]) -> dict[str, object]:
        if len({group.id for group in groups}) != len(groups):
            raise ValueError("duplicate initial tool group id")
        return {group.id: self._retain(group)[1] for group in groups}

    def _retain(self, group: ToolGroupContribution) -> tuple[str, dict[str, object]]:
        identity = uuid4().hex
        tools = {tool.id: tool for tool in group.tools}
        if len(tools) != len(group.tools):
            raise ValueError("duplicate tool id in group")
        descriptors = [
            {
                **{
                    field.name: getattr(tool, field.name)
                    for field in fields(tool)
                    if field.name not in {"handler", "resume_handler"}
                },
                "parameters_schema": dict(tool.parameters_schema),
                "result_schema": dict(tool.result_schema),
                "resumable": tool.resume_handler is not None,
            }
            for tool in group.tools
        ]
        self._handlers[identity] = tools
        return identity, {
            "id": group.id,
            "revision": group.revision,
            "registration_id": identity,
            "tools": descriptors,
        }

    async def publish(self, group: ToolGroupContribution) -> WorkerToolGroupRegistration:
        identity, descriptor = self._retain(group)
        try:
            await self.peer.request("tool_groups.publish", descriptor)
        except BaseException:
            # A cancelled response does not prove that publication failed. The
            # Host withdraws by identity and keeps accepted calls' handlers alive.
            retained = await self.peer.close_registration(identity, method="tool_groups.close")
            if not retained:
                self._handlers.pop(identity, None)
            raise
        return WorkerToolGroupRegistration(self, identity)

    async def withdraw(self, identity: str) -> None:
        if identity not in self._handlers:
            return
        retained = await self.peer.request("tool_groups.close", {"registration_id": identity})
        if not retained:
            self._handlers.pop(identity, None)

    def release(self, params: Mapping[str, Any]) -> None:
        self._handlers.pop(params["registration_id"], None)

    async def call(self, params: Mapping[str, Any]) -> dict[str, Any]:
        tool = self._handlers[params["registration_id"]][params["contribution_id"]]
        return await invoke_worker_tool(tool, params)

    async def resume(self, params: Mapping[str, Any]) -> dict[str, Any]:
        tool = self._handlers[params["registration_id"]][params["contribution_id"]]
        return await invoke_worker_tool(tool, params, resume=True)
