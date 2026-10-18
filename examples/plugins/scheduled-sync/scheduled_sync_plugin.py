from __future__ import annotations

import json
from datetime import UTC, datetime

from ouro.sdk.plugins import v5 as sdk
from ouro.sdk.plugins.scheduling import PluginScheduler, ScheduledService, TaskSchedule, TaskSpec
from ouro.sdk.plugins.services import ServiceCall, ServiceContribution

PLUGIN_ID = "dev.ouro.examples.scheduled_sync"


def validate_snapshot(content: bytes) -> None:
    snapshot = json.loads(content)
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ValueError("Expected snapshot schema_version 1; maintain data while disabled")
    records = snapshot.get("records")
    if not isinstance(records, dict) or any(
        not isinstance(key, str) or not key or not isinstance(value, str)
        for key, value in records.items()
    ):
        raise ValueError("Snapshot records must map nonempty ids to string text")
    if not isinstance(snapshot.get("execution_id"), str) or not snapshot["execution_id"]:
        raise ValueError("Snapshot execution_id must be a nonempty string")
    if not isinstance(snapshot.get("scheduled_at"), str):
        raise ValueError("Snapshot scheduled_at must be an ISO datetime")
    if datetime.fromisoformat(snapshot["scheduled_at"]).tzinfo is None:
        raise ValueError("Snapshot scheduled_at must include a timezone")


class ScheduledSync:
    def __init__(self, scheduler: PluginScheduler, data: sdk.PluginDataStore) -> None:
        self.scheduler = scheduler
        self.data = data

    async def command(self, call: sdk.CommandCall) -> sdk.CommandOutput:
        parts = call.argument.split()
        if parts == ["create"]:
            task = await self.scheduler.create(
                TaskSpec(
                    ScheduledService(PLUGIN_ID, "sync", "run"),
                    TaskSchedule("hourly", starts_at=datetime.now(UTC)),
                    title="Synchronize local records",
                )
            )
            return sdk.CommandOutput(output=f"Created {task.task_id}")
        if parts == ["list"]:
            tasks = await self.scheduler.list(limit=50)
            return sdk.CommandOutput(
                output="\n".join(
                    f"{task.task_id}: {task.status}, revision {task.revision}" for task in tasks
                )
                or "No synchronization plans."
            )
        if len(parts) == 2 and parts[0] in {"pause", "resume", "delete"}:
            action, task_id = parts
            task = await self.scheduler.get(task_id)
            if action == "delete":
                await self.scheduler.delete(task_id, expected_revision=task.revision)
            else:
                await self.scheduler.set_enabled(
                    task_id, action == "resume", expected_revision=task.revision
                )
            return sdk.CommandOutput(output=f"{action}: {task_id}")
        raise ValueError("Use create, list, pause <id>, resume <id>, or delete <id>")

    def sync(self, call: ServiceCall) -> sdk.JsonValue:
        if call.scheduled is None:
            raise ValueError("Sync requires a scheduled invocation")
        source = json.loads(self.data.read_bytes("inbox.json"))
        if not isinstance(source, dict) or source.get("schema_version") != 1:
            raise ValueError("Expected inbox schema_version 1")
        records = source.get("records")
        if not isinstance(records, list) or any(
            not isinstance(record, dict)
            or not isinstance(record.get("id"), str)
            or not record["id"]
            or not isinstance(record.get("text"), str)
            for record in records
        ):
            raise ValueError("Records must contain nonempty string ids and string text")
        values = {record["id"]: record["text"] for record in records}
        if len(values) != len(records):
            raise ValueError("Record ids must be unique")
        snapshot = {
            "schema_version": 1,
            "records": values,
            "execution_id": call.scheduled.execution_id,
            "scheduled_at": call.scheduled.scheduled_at.isoformat(),
        }
        self.data.write_bytes(
            "snapshot.json", json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        )
        return {"records": len(values)}

    async def prepare(self) -> sdk.ContributionSet:
        try:
            snapshot = self.data.read_bytes("snapshot.json")
        except FileNotFoundError:
            pass
        else:
            validate_snapshot(snapshot)
        return sdk.ContributionSet(
            commands=[
                sdk.CommandContribution("sync-plan", "Manage synchronization plans.", self.command)
            ],
            services=[ServiceContribution("sync", {"run": self.sync})],
        )

    async def start(self) -> None:
        pass

    async def stop(self, reason: sdk.StopReason, deadline: float) -> None:
        pass


def create_plugin(context: sdk.PluginContext) -> ScheduledSync:
    if context.scheduler is None:
        raise RuntimeError("This Host does not provide persistent scheduling")
    return ScheduledSync(context.scheduler, context.data)
