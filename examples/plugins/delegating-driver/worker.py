from __future__ import annotations

import asyncio

from delegating_driver_plugin import delegate

from ouro.sdk.plugins import v3 as sdk
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_drivers import WorkerRunDrivers


class Worker:
    def __init__(self) -> None:
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "drivers.call": self.drive,
            },
            max_requests=1,
        )
        self.drivers = WorkerRunDrivers(
            self.peer, [sdk.RunDriverContribution("delegate", delegate)]
        )

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"run_drivers": ["delegate"]}

    async def lifecycle(self, params):
        return {}

    async def drive(self, params):
        return await self.drivers.dispatch("drivers.call", params)


asyncio.run(Worker().peer.serve())
