from ouro.sdk.plugins.v4 import ContributionSet, ObserverContribution, RunFinishedObservation


class ObserverExamplePlugin:
    def __init__(self, context) -> None:
        self.context = context

    async def prepare(self) -> ContributionSet:
        def observe(observation) -> None:
            if isinstance(observation, RunFinishedObservation):
                self.context.logger.info(
                    "run finished: status=%s origin=%s",
                    observation.outcome.status,
                    observation.outcome.origin,
                )

        return ContributionSet(
            observers=[
                ObserverContribution(
                    id="run_completion",
                    event="run_finished",
                    handler=observe,
                )
            ]
        )

    async def start(self) -> None:
        return None

    async def stop(self, reason, deadline) -> None:
        del reason, deadline


def create_plugin(context) -> ObserverExamplePlugin:
    return ObserverExamplePlugin(context)
