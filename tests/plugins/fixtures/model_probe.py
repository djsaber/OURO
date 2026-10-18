import asyncio
from dataclasses import asdict

from ouro.sdk.plugins import v4 as sdk
from ouro.sdk.plugins.models import ModelInput, ModelRequest
from ouro.sdk.plugins.services import ServiceCall
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_models import WorkerModels

PREPARE_REQUEST = False


class Probe:
    def __init__(self, models):
        self.models = models

    async def generate(self, call):
        arguments = call.arguments or {}
        chunks = []

        async def output(delta):
            if arguments.get("callback_error"):
                raise ValueError("consumer failed")
            chunks.append(asdict(delta))

        result = await self.models.generate(
            ModelRequest(
                messages=(ModelInput("user", "Review"),),
                model="faux/faux",
                timeout_seconds=arguments.get("timeout_seconds", 5),
            ),
            on_output=output,
        )
        return {"result": asdict(result), "output": chunks}

    async def prepare(self):
        if PREPARE_REQUEST:
            await self.generate(ServiceCall("prepare", {}))
        return sdk.ContributionSet(
            services=[sdk.ServiceContribution("probe", {"generate": self.generate})]
        )

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


def create_plugin(context):
    return Probe(context.models)


class Worker:
    def __init__(self):
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "services.call": self.call,
            },
            max_requests=1,
            request_timeout=0.01,
        )
        self.models = WorkerModels(self.peer)
        self.peer.handlers["models.output"] = self.models.output
        self.plugin = Probe(self.models)

    async def initialize(self, params):
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        await self.plugin.prepare()
        return {"services": ["probe"]}

    async def lifecycle(self, params):
        return {}

    async def call(self, params):
        return await self.plugin.generate(ServiceCall(params["caller_id"], params.get("arguments")))


if __name__ == "__main__":
    asyncio.run(Worker().peer.serve())
