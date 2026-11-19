from __future__ import annotations

import pytest

from ouro.core.context_providers import (
    ContextFragment,
    ContextObservation,
    ContextProviderCall,
    ContextProviderRegistration,
    ContextProviderRegistry,
    ContextProviderResult,
)
from ouro.core.system_prompts import (
    SystemPromptFragment,
    SystemPromptRegistration,
    SystemPromptRegistry,
)


def _call() -> ContextProviderCall:
    return ContextProviderCall(
        session_id="ses_1",
        run_id="run_1",
        message_count=3,
        model_round=2,
        workspace="/workspace",
    )


async def test_scoped_prompt_providers_keep_the_rounds_order_and_controller_selection():
    registry = SystemPromptRegistry()
    rules = SystemPromptRegistration(
        "plugin", "rules", lambda _: SystemPromptFragment(text="rules")
    )
    mode = SystemPromptRegistration(
        "plugin",
        "mode",
        lambda _: SystemPromptFragment(text="mode"),
        priority=1,
        active_when_controller_id="plan",
    )
    registry.replace_owner("plugin", (rules, mode))
    active = registry.scoped({"plan"})
    inactive = registry.scoped(set())
    registry.replace_owner("plugin", ())

    assert (await active.evaluate(None)).fragments == ["mode", "rules"]
    assert (await inactive.evaluate(None)).fragments == ["rules"]
    assert (await registry.evaluate(None)).fragments == []


@pytest.mark.asyncio
async def test_context_providers_order_by_priority_and_identity() -> None:
    registry = ContextProviderRegistry()
    registry.replace_owner(
        "providers",
        (
            ContextProviderRegistration(
                plugin_id="dev.ouro.zeta",
                provider_id="later",
                priority=20,
                handler=lambda _call: ContextProviderResult(
                    fragments=[ContextFragment(text="later")]
                ),
            ),
            ContextProviderRegistration(
                plugin_id="dev.ouro.alpha",
                provider_id="first",
                generation_id="gen_alpha",
                priority=10,
                handler=lambda _call: ContextProviderResult(
                    fragments=[
                        ContextObservation(
                            text="first",
                            semantic_tag="plugin-context",
                            plugin_id="previous.plugin",
                            provider_id="previous-provider",
                            fragment_index=4,
                        )
                    ]
                ),
            ),
        ),
    )

    result = await registry.evaluate(_call())

    assert result.fragments == [
        ContextObservation(
            text="first",
            semantic_tag="plugin-context",
            plugin_id="dev.ouro.alpha",
            provider_id="first",
            generation_id="gen_alpha",
            fragment_index=0,
        ),
        ContextObservation(
            text="later", plugin_id="dev.ouro.zeta", provider_id="later", fragment_index=0
        ),
    ]
    assert result.failures == []
    assert result.blocked is False


@pytest.mark.asyncio
async def test_context_provider_omit_and_block_are_distinct_failure_contracts() -> None:
    registry = ContextProviderRegistry()

    def fail_omit(_call):
        raise RuntimeError("token=omit-secret")

    def fail_block(_call):
        raise RuntimeError("token=block-secret")

    registry.replace_owner(
        "providers",
        (
            ContextProviderRegistration(
                plugin_id="dev.ouro.omit",
                provider_id="optional",
                priority=1,
                failure_mode="omit",
                handler=fail_omit,
            ),
            ContextProviderRegistration(
                plugin_id="dev.ouro.good",
                provider_id="available",
                priority=2,
                handler=lambda _call: ContextProviderResult(
                    fragments=[ContextFragment(text="available")]
                ),
            ),
            ContextProviderRegistration(
                plugin_id="dev.ouro.block",
                provider_id="required",
                priority=3,
                failure_mode="block",
                handler=fail_block,
            ),
            ContextProviderRegistration(
                plugin_id="dev.ouro.never",
                provider_id="after-block",
                priority=4,
                handler=lambda _call: ContextProviderResult(
                    fragments=[ContextFragment(text="must not run")]
                ),
            ),
        ),
    )

    result = await registry.evaluate(_call())

    assert [fragment.text for fragment in result.fragments] == ["available"]
    assert result.blocked is True
    assert [failure.failure_mode for failure in result.failures] == ["omit", "block"]
    assert [failure.message for failure in result.failures] == ["token=***", "token=***"]
