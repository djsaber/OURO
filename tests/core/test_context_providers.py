from __future__ import annotations

import pytest

from ouro.core.context_providers import (
    ContextFragment,
    ContextProviderCall,
    ContextProviderRegistration,
    ContextProviderRegistry,
    ContextProviderResult,
)


def _call() -> ContextProviderCall:
    return ContextProviderCall(
        session_id="ses_1",
        run_id="run_1",
        message_count=3,
        model_round=2,
        workspace="/workspace",
    )


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
                    fragments=[
                        ContextFragment(text="later", target="system_prompt")
                    ]
                ),
            ),
            ContextProviderRegistration(
                plugin_id="dev.ouro.alpha",
                provider_id="first",
                generation_id="gen_alpha",
                priority=10,
                handler=lambda _call: ContextProviderResult(
                    fragments=[
                        ContextFragment(
                            text="first",
                            persistence="message",
                            semantic_tag="plugin-context",
                        )
                    ]
                ),
            ),
        ),
    )

    result = await registry.evaluate(_call())

    assert [fragment.text for fragment in result.fragments] == ["first", "later"]
    assert result.fragments[0].plugin_id == "dev.ouro.alpha"
    assert result.fragments[0].provider_id == "first"
    assert result.fragments[0].generation_id == "gen_alpha"
    assert result.fragments[0].fragment_index == 0
    assert result.fragments[0].persistence == "message"
    assert result.fragments[0].semantic_tag == "plugin-context"
    assert result.fragments[1].semantic_tag == "system-reminder"
    assert result.fragments[1].target == "system_prompt"
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
