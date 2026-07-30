import asyncio

import pytest

from ouro.core.prompt_policies import (
    PromptPolicyCall,
    PromptPolicyDecision,
    PromptPolicyRegistration,
    PromptPolicyRegistry,
)


def _call() -> PromptPolicyCall:
    return PromptPolicyCall(
        session_id="ses_1",
        prospective_run_id="run_1",
        prompt="inspect this prompt",
    )


@pytest.mark.asyncio
async def test_prompt_policies_are_stably_ordered_and_deny_short_circuits() -> None:
    calls: list[str] = []

    def allow(_call: PromptPolicyCall) -> PromptPolicyDecision:
        calls.append("allow")
        return PromptPolicyDecision()

    def deny(_call: PromptPolicyCall) -> PromptPolicyDecision:
        calls.append("deny")
        return PromptPolicyDecision(action="deny", reason="blocked")

    def unreachable(_call: PromptPolicyCall) -> PromptPolicyDecision:
        calls.append("unreachable")
        return PromptPolicyDecision()

    registry = PromptPolicyRegistry()
    registry.register_many(
        (
            PromptPolicyRegistration("z.plugin", "last", unreachable, priority=20),
            PromptPolicyRegistration("b.plugin", "deny", deny, priority=10),
            PromptPolicyRegistration("a.plugin", "allow", allow, priority=10),
        )
    )

    evaluation = await registry.evaluate(_call())

    assert evaluation.allowed is False
    assert evaluation.reason == "blocked"
    assert evaluation.rejected_by is not None
    assert evaluation.rejected_by.plugin_id == "b.plugin"
    assert calls == ["allow", "deny"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [None, "allow"])
async def test_invalid_prompt_policy_return_is_fail_closed(result: object) -> None:
    registry = PromptPolicyRegistry()
    registry.register(
        PromptPolicyRegistration(
            "dev.ouro.policy",
            "invalid",
            lambda _call: result,  # type: ignore[arg-type,return-value]
        )
    )

    evaluation = await registry.evaluate(_call())

    assert evaluation.allowed is False
    assert evaluation.reason == "prompt policy evaluation failed"
    assert evaluation.failures[0].error_type == "TypeError"


@pytest.mark.asyncio
async def test_prompt_policy_timeout_is_fail_closed_and_redacts_failures() -> None:
    async def timeout(_call: PromptPolicyCall) -> PromptPolicyDecision:
        await asyncio.sleep(1)
        return PromptPolicyDecision()

    registry = PromptPolicyRegistry()
    registry.register_many(
        (
            PromptPolicyRegistration(
                "dev.ouro.policy",
                "timeout",
                timeout,
                timeout_seconds=0.01,
            ),
        )
    )
    evaluation = await registry.evaluate(_call())
    assert evaluation.allowed is False
    assert evaluation.failures[0].error_type == "TimeoutError"

    def explode(_call: PromptPolicyCall) -> PromptPolicyDecision:
        raise RuntimeError("token=policy-secret")

    registry = PromptPolicyRegistry()
    registry.register(PromptPolicyRegistration("dev.ouro.policy", "explode", explode))
    evaluation = await registry.evaluate(_call())
    assert evaluation.allowed is False
    assert evaluation.failures[0].message == "token=***"


def test_prompt_policy_decisions_require_consistent_reasons() -> None:
    with pytest.raises(ValueError, match="requires a reason"):
        PromptPolicyDecision(action="deny")
    with pytest.raises(ValueError, match="cannot contain a reason"):
        PromptPolicyDecision(action="allow", reason="not applicable")
