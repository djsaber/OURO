from ouro.sdk.plugins import v5 as sdk
from ouro.sdk.plugins.services import ServiceContribution


class Plugin:
    def __init__(self, context):
        self.context = context
        self.background_started = False

    async def read(self, call):
        return "original:" + self.context.configuration["label"]

    async def prepare(self):
        return sdk.ContributionSet(
            services=[ServiceContribution("value", {"read": self.read})],
            background_services=[
                sdk.BackgroundServiceContribution(
                    "watcher", self.start_background, self.stop_background
                )
            ],
        )

    async def start_background(self):
        self.background_started = True

    async def stop_background(self):
        self.background_started = False

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


def create(context):
    return Plugin(context)
