from __future__ import annotations

import asyncio
from datetime import datetime

from ouro.sdk.plugins import v4 as sdk
from ouro.sdk.plugins.scheduled import ScheduledInvocation
from ouro.sdk.plugins.services import ServiceCall, ServiceContribution
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_scheduling import (
    WorkerScheduler,
    decode_spec,
    encode_execution,
    encode_task,
)

LABEL = "one"


class Probe:
    def __init__(self, scheduler=None):
        self.scheduler = scheduler
        self.calls = 0
        self.released = asyncio.Event()
        self.methods = {
            "run": self.run,
            "inspect": self.inspect,
            "release": self.release,
            "schedule": self.schedule,
            "read_schedule": self.schedule,
        }

    async def run(self, call):
        assert call.scheduled is not None
        self.calls += 1
        if call.arguments.get("block"):
            await self.released.wait()
        if call.arguments.get("fail"):
            raise ValueError("Sync failed")
        if call.arguments.get("mutate"):
            call.arguments["handler_value"] = "local"
        return {
            "label": LABEL,
            "calls": self.calls,
            "task_id": call.scheduled.task_id,
            "execution_id": call.scheduled.execution_id,
            "scheduled_at": call.scheduled.scheduled_at.isoformat(),
            "arguments": call.arguments,
            "caller_id": call.caller_id,
        }

    async def inspect(self, call):
        return self.calls

    async def release(self, call):
        self.released.set()

    async def schedule(self, call):
        assert self.scheduler is not None
        params = dict(call.arguments)
        operation = params.pop("operation")
        if operation in {"create", "update"}:
            params["configuration"] = decode_spec(params["configuration"])
        if operation not in {
            "create",
            "update",
            "get",
            "list",
            "set_enabled",
            "delete",
            "executions",
            "interrupt",
        }:
            raise ValueError("Unknown scheduling operation")
        result = await getattr(self.scheduler, operation)(**params)
        if operation in {"create", "update", "get", "set_enabled"}:
            return encode_task(result)
        if operation == "list":
            return [encode_task(item) for item in result]
        if operation == "executions":
            return [encode_execution(item) for item in result]
        return result

    async def prepare(self):
        return sdk.ContributionSet(services=[ServiceContribution("sync", self.methods)])

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


def create_plugin(context):
    return Probe(context.scheduler)


class Worker:
    def __init__(self):
        self.probe = Probe()
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.start,
                "lifecycle.stop": self.lifecycle,
                "services.call": self.call,
            }
        )
        self.probe.scheduler = WorkerScheduler(self.peer)

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"services": ["sync"]}

    async def lifecycle(self, params):
        return {}

    async def start(self, params):
        await self.probe.start()
        return {}

    async def call(self, params):
        scheduled = params.get("scheduled")
        call = ServiceCall(
            params["caller_id"],
            params.get("arguments"),
            scheduled=ScheduledInvocation(
                scheduled["task_id"],
                scheduled["execution_id"],
                datetime.fromisoformat(scheduled["scheduled_at"]),
            )
            if scheduled
            else None,
        )
        return await self.probe.methods[params["method"]](call)


if __name__ == "__main__":
    asyncio.run(Worker().peer.serve())
