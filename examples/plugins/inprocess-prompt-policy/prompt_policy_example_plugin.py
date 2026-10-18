from ouro.sdk.plugins.v5 import (
    ContributionSet,
    PromptPolicyContribution,
    PromptPolicyDecision,
)


class PromptPolicyExamplePlugin:
    async def prepare(self) -> ContributionSet:
        def inspect_prompt(call) -> PromptPolicyDecision:
            if "DEMO_SECRET=" in call.prompt:
                return PromptPolicyDecision(
                    action="deny",
                    reason="Remove the demonstration secret marker before submitting.",
                )
            return PromptPolicyDecision()

        return ContributionSet(
            prompt_policies=[
                PromptPolicyContribution(
                    id="demo_secret_guard",
                    handler=inspect_prompt,
                )
            ]
        )

    async def start(self) -> None:
        return None

    async def stop(self, reason, deadline) -> None:
        del reason, deadline


def create_plugin(context) -> PromptPolicyExamplePlugin:
    del context
    return PromptPolicyExamplePlugin()
