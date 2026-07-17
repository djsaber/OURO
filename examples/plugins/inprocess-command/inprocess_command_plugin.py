from __future__ import annotations

from ouro.sdk.plugins.v1 import CommandContribution, ContributionSet


class Plugin:
    async def prepare(self) -> ContributionSet:
        async def hello(call):
            subject = call.argument.strip() or "OURO"
            return f"Hello, {subject}!"

        return ContributionSet(
            commands=[
                CommandContribution(
                    id="hello",
                    description="Return a greeting.",
                    handler=hello,
                )
            ]
        )

    async def start(self) -> None:
        return None

    async def stop(self, reason, deadline) -> None:
        del reason, deadline


def create_plugin(context) -> Plugin:
    del context
    return Plugin()
