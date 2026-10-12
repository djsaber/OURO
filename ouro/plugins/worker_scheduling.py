from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ouro.core.tasks import TaskConfiguration
from ouro.plugins.scheduling import task_spec
from ouro.sdk.plugins.scheduling import PluginScheduler
from ouro.sdk.plugins.worker_scheduling import encode_execution, encode_task


class WorkerSchedulerBridge:
    def __init__(self, scheduler: PluginScheduler) -> None:
        self.scheduler = scheduler

    async def dispatch(self, method: str, params: Mapping[str, object]) -> object:
        arguments: dict[str, Any] = dict(params)
        if method in {"scheduler.create", "scheduler.update"}:
            arguments["configuration"] = task_spec(
                TaskConfiguration.model_validate(arguments["configuration"])
            )
        if method == "scheduler.create":
            return encode_task(await self.scheduler.create(**arguments))
        if method == "scheduler.update":
            return encode_task(await self.scheduler.update(**arguments))
        if method == "scheduler.get":
            return encode_task(await self.scheduler.get(**arguments))
        if method == "scheduler.list":
            return [encode_task(item) for item in await self.scheduler.list(**arguments)]
        if method == "scheduler.set_enabled":
            return encode_task(await self.scheduler.set_enabled(**arguments))
        if method == "scheduler.executions":
            return [encode_execution(item) for item in await self.scheduler.executions(**arguments)]
        if method == "scheduler.delete":
            await self.scheduler.delete(**arguments)
            return None
        if method == "scheduler.interrupt":
            await self.scheduler.interrupt(**arguments)
            return None
        raise ValueError(f"Unsupported scheduling operation: {method}")
