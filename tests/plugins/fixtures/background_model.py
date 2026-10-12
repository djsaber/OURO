"""A background service which initializes through its own public Provider."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from ouro.sdk.plugins import v3 as sdk
from ouro.sdk.plugins.models import ModelInput, ModelRequest
from ouro.sdk.plugins.providers import (
    ProviderCheck,
    ProviderCompletion,
    ProviderContribution,
    ProviderEvent,
)
from ouro.sdk.plugins.worker import WorkerPeer
from ouro.sdk.plugins.worker_models import WorkerModels
from ouro.sdk.plugins.worker_providers import WorkerProviders


class Plugin:
    def __init__(self, models, configuration):
        self.models = models
        self.configuration = configuration
        self.identity = uuid4().hex

    def record(self, phase):
        with Path(self.configuration["events"]).open("a", encoding="utf-8") as output:
            output.write(json.dumps([phase, self.identity]) + "\n")

    async def stream(self, request, output):
        self.record("request")
        await output(ProviderEvent("text_delta", text=self.identity))
        await output(ProviderEvent("done", completion=ProviderCompletion("stop")))

    async def check(self, request):
        return ProviderCheck(True, "ready")

    async def start_background(self):
        self.record("starting")
        release = self.configuration.get("release")
        if release:
            while not Path(release).exists():
                await asyncio.sleep(0.01)
        result = await self.models.generate(
            ModelRequest((ModelInput("user", "initialize"),), timeout_seconds=2)
        )
        assert result.text == self.identity
        self.record("started")

    async def stop_background(self):
        marker = self.configuration.get("fail_stop")
        if marker and Path(marker).exists():
            raise RuntimeError("service still owns its resource")
        self.record("stopped")

    async def prepare(self):
        return sdk.ContributionSet(
            providers=[ProviderContribution("probe", self.stream, self.check)],
            background_services=[
                sdk.BackgroundServiceContribution(
                    "initialize", self.start_background, self.stop_background
                )
            ],
        )

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


def create_plugin(context):
    return Plugin(context.models, context.configuration)


class Worker:
    def __init__(self):
        self.peer = WorkerPeer(
            {
                "initialize": self.initialize,
                "contributions.prepare": self.prepare,
                "lifecycle.start": self.lifecycle,
                "lifecycle.stop": self.lifecycle,
                "background.start": self.start_background,
                "background.stop": self.stop_background,
            }
        )
        self.models = WorkerModels(self.peer)
        self.peer.handlers["models.output"] = self.models.output
        self.providers = WorkerProviders(self.peer)

    async def initialize(self, params):
        self.plugin = Plugin(self.models, params["configuration"])
        return {"protocol_version": "3.0"}

    async def prepare(self, params):
        contributions = await self.plugin.prepare()
        return {
            "providers": self.providers.register(contributions.providers),
            "background_services": ["initialize"],
        }

    async def lifecycle(self, params):
        return {}

    async def start_background(self, params):
        await self.plugin.start_background()

    async def stop_background(self, params):
        await self.plugin.stop_background()


if __name__ == "__main__":
    asyncio.run(Worker().peer.serve())
