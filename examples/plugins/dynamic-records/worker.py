from __future__ import annotations

import asyncio
from dataclasses import asdict

from dynamic_records_plugin import record_group, set_fields

from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_tool_groups import WorkerToolGroups


class Worker:
    def __init__(self) -> None:
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "command.call": self.command,
            },
            max_requests=1,
        )
        self.groups = WorkerToolGroups(self.peer)

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"commands": ["record-fields"], "tool_groups": self.groups.prepare([record_group()])}

    async def command(self, params):
        return asdict(await set_fields(self.groups, params["call"]["argument"]))

    async def lifecycle(self, params):
        return {}


if __name__ == "__main__":
    asyncio.run(Worker().peer.serve())
