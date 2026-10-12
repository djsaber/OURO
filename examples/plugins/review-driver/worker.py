from __future__ import annotations

import asyncio
from dataclasses import asdict

from review_driver_plugin import request_review, review

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
                "command.call": self.command,
            },
            max_requests=1,
        )
        self.drivers = WorkerRunDrivers(self.peer, [sdk.RunDriverContribution("review", review)])

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        return {"run_drivers": ["review"], "commands": ["review"]}

    async def lifecycle(self, params):
        return {}

    async def drive(self, params):
        return await self.drivers.dispatch("drivers.call", params)

    async def command(self, params):
        call = params["call"]
        return asdict(
            request_review(
                sdk.CommandCall(
                    command_id=call["command_id"],
                    argument=call["argument"],
                    session_id=call["session_id"],
                )
            )
        )


asyncio.run(Worker().peer.serve())
