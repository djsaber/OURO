"""Persistent task commands over the public worker protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, time
from typing import Any

from .models import ModelReasoning
from .scheduling import (
    ScheduledAgent,
    ScheduledExecution,
    ScheduledService,
    ScheduledTask,
    TaskSchedule,
    TaskSpec,
)
from .worker import WorkerPeer


def encode_spec(value: TaskSpec) -> dict[str, Any]:
    raw = asdict(value)
    schedule = raw["schedule"]
    schedule["weekdays"] = list(value.schedule.weekdays)
    for key in ("starts_at", "ends_at", "at"):
        if schedule[key] is not None:
            schedule[key] = schedule[key].isoformat()
    return raw


def decode_spec(raw: Mapping[str, Any]) -> TaskSpec:
    target = dict(raw["target"])
    kind = target.pop("kind")
    action: ScheduledAgent | ScheduledService
    if kind == "agent":
        target["reasoning"] = ModelReasoning(**target.get("reasoning", {}))
        action = ScheduledAgent(**target)
    elif kind == "plugin":
        action = ScheduledService(**target)
    else:
        raise ValueError("Unsupported scheduled target kind")
    schedule = dict(raw["schedule"])
    schedule["starts_at"] = datetime.fromisoformat(schedule["starts_at"])
    if schedule.get("ends_at") is not None:
        schedule["ends_at"] = datetime.fromisoformat(schedule["ends_at"])
    if "at" in schedule:
        schedule["at"] = time.fromisoformat(schedule["at"])
    if "weekdays" in schedule:
        schedule["weekdays"] = tuple(schedule["weekdays"])
    return TaskSpec(action, TaskSchedule(**schedule), raw.get("title"))


def encode_task(value: ScheduledTask) -> dict[str, Any]:
    return {
        "task_id": value.task_id,
        "revision": value.revision,
        "configuration": encode_spec(value.configuration),
        "enabled": value.enabled,
        "status": value.status,
        "next_run_at": value.next_run_at.isoformat() if value.next_run_at is not None else None,
        "active_execution_id": value.active_execution_id,
        "error": value.error,
    }


def decode_task(raw: Mapping[str, Any]) -> ScheduledTask:
    values = dict(raw)
    values["configuration"] = decode_spec(values["configuration"])
    if values["next_run_at"] is not None:
        values["next_run_at"] = datetime.fromisoformat(values["next_run_at"])
    return ScheduledTask(**values)


def encode_execution(value: ScheduledExecution) -> dict[str, Any]:
    raw = asdict(value)
    raw["scheduled_at"] = value.scheduled_at.isoformat()
    raw["ended_at"] = value.ended_at.isoformat() if value.ended_at is not None else None
    return raw


def decode_execution(raw: Mapping[str, Any]) -> ScheduledExecution:
    values = dict(raw)
    values["scheduled_at"] = datetime.fromisoformat(values["scheduled_at"])
    if values["ended_at"] is not None:
        values["ended_at"] = datetime.fromisoformat(values["ended_at"])
    return ScheduledExecution(**values)


class WorkerScheduler:
    def __init__(self, peer: WorkerPeer) -> None:
        self.peer = peer

    async def create(self, configuration: TaskSpec) -> ScheduledTask:
        return decode_task(
            await self.peer.request(
                "scheduler.create", {"configuration": encode_spec(configuration)}, timeout=0
            )
        )

    async def update(
        self, task_id: str, configuration: TaskSpec, *, expected_revision: int
    ) -> ScheduledTask:
        return decode_task(
            await self.peer.request(
                "scheduler.update",
                {
                    "task_id": task_id,
                    "configuration": encode_spec(configuration),
                    "expected_revision": expected_revision,
                },
                timeout=0,
            )
        )

    async def get(self, task_id: str) -> ScheduledTask:
        return decode_task(
            await self.peer.request("scheduler.get", {"task_id": task_id}, timeout=0)
        )

    async def list(self, *, limit: int = 200, offset: int = 0) -> tuple[ScheduledTask, ...]:
        return tuple(
            decode_task(item)
            for item in await self.peer.request(
                "scheduler.list", {"limit": limit, "offset": offset}, timeout=0
            )
        )

    async def set_enabled(
        self, task_id: str, enabled: bool, *, expected_revision: int
    ) -> ScheduledTask:
        return decode_task(
            await self.peer.request(
                "scheduler.set_enabled",
                {
                    "task_id": task_id,
                    "enabled": enabled,
                    "expected_revision": expected_revision,
                },
                timeout=0,
            )
        )

    async def delete(self, task_id: str, *, expected_revision: int) -> None:
        await self.peer.request(
            "scheduler.delete",
            {"task_id": task_id, "expected_revision": expected_revision},
            timeout=0,
        )

    async def executions(self, task_id: str, *, limit: int = 50) -> tuple[ScheduledExecution, ...]:
        return tuple(
            decode_execution(item)
            for item in await self.peer.request(
                "scheduler.executions", {"task_id": task_id, "limit": limit}, timeout=0
            )
        )

    async def interrupt(self, task_id: str, *, expected_revision: int) -> None:
        await self.peer.request(
            "scheduler.interrupt",
            {"task_id": task_id, "expected_revision": expected_revision},
            timeout=0,
        )
