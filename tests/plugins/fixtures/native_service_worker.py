import asyncio

from review_driver_plugin import Driver

from ouro.sdk.plugins import v3 as sdk
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_drivers import WorkerRunDrivers
from ouro.sdk.plugins.worker_services import WorkerServices


class Worker:
    def __init__(self):
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
        driver = Driver(WorkerServices(self.peer))
        self.drivers = WorkerRunDrivers(
            self.peer, [sdk.RunDriverContribution("review", driver.run)]
        )

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"run_drivers": ["review"]}

    async def lifecycle(self, params):
        return {}

    async def drive(self, params):
        return await self.drivers.dispatch("drivers.call", params)


asyncio.run(Worker().peer.serve())
