import asyncio

from ouro.sdk.plugins import v4 as sdk


class ReviewPlugin:
    def __init__(self, context):
        self.context = context
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def review(self, context, resume):
        if resume is None:
            result = await context.tools.execute((sdk.RunToolCall("Deploy"),), checkpoint="finish")
            assert result.suspended
            return sdk.DriverSuspended()
        value = await self.context.services.call("dev.ouro.driver_dependency", "value", "read")
        self.entered.set()
        if self.context.configuration.get("wait"):
            await self.release.wait()
        return sdk.DriverFinished(self.context.configuration["label"] + "/" + value)

    async def prepare(self):
        return sdk.ContributionSet(run_drivers=[sdk.RunDriverContribution("review", self.review)])

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


def create_plugin(context):
    return ReviewPlugin(context)
