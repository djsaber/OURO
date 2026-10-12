from __future__ import annotations

import asyncio

from review_service_plugin import ReviewService

from ouro.sdk.plugins.services import ServiceCall
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_drivers import WorkerRunPorts


class Worker:
    def __init__(self) -> None:
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "services.call": self.call,
            },
            max_requests=1,
        )
        ports = WorkerRunPorts(self.peer)
        self.service = ReviewService(ports.models, ports.tools)

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"services": ["review"]}

    async def lifecycle(self, params):
        return {}

    async def call(self, params):
        methods = {"inspect": self.service.inspect, "act": self.service.act}
        return await methods[params["method"]](
            ServiceCall(caller_id=params["caller_id"], arguments=params.get("arguments"))
        )


asyncio.run(Worker().peer.serve())
