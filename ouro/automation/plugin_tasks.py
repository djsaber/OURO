from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import JsonValue

from ouro.core.plugin_tasks import PluginTaskCall, PluginTaskTarget
from ouro.core.tasks import AgentTaskTarget


class BoundPluginTask(Protocol):
    """One accepted service implementation, held only until execution settles."""

    @property
    def target(self) -> PluginTaskCall: ...

    async def invoke(
        self, *, task_id: str, execution_id: str, scheduled_at: datetime
    ) -> JsonValue: ...

    def close(self) -> None: ...


class PluginTaskProvider(Protocol):
    async def bind_task(self, target: PluginTaskTarget) -> BoundPluginTask: ...

    def task_owner_available(self, owner_id: str) -> bool: ...

    def require_task_owner(
        self, owner_id: str, target: AgentTaskTarget | PluginTaskTarget
    ) -> None: ...
