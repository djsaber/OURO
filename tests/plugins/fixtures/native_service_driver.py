from ouro.sdk.plugins import v5 as sdk

SERVICE = "dev.ouro.examples.review_service"


class Driver:
    def __init__(self, services):
        self.services = services

    async def run(self, context, resume):
        if resume is not None:
            return sdk.DriverFinished("\n".join(item.content for item in resume.results))
        review = await self.services.call(SERVICE, "review", "inspect")
        action = await self.services.call(SERVICE, "review", "act", review["text"])
        if action["suspended"]:
            return sdk.DriverSuspended()
        return sdk.DriverFinished(action["text"], response_id=action.get("response_id"))

    async def prepare(self):
        return sdk.ContributionSet(run_drivers=[sdk.RunDriverContribution("review", self.run)])

    async def start(self):
        pass

    async def stop(self, reason, deadline):
        pass


def create_plugin(context):
    return Driver(context.services)
