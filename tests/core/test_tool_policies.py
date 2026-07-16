from __future__ import annotations

import asyncio

import pytest

from ouro.core.tool_policies import (
    ToolPolicyCall,
    ToolPolicyDecision,
    ToolPolicyRegistration,
    ToolPolicyRegistry,
)


def _call() -> ToolPolicyCall:
    return ToolPolicyCall(
        session_id="ses_1",
        run_id="run_1",
        tool_call_id="tool_1",
        tool_name="Shell",
        arguments={"command": "rm -rf build"},
        workspace="/workspace",
    )


@pytest.mark.asyncio
async def test_tool_policies_compose_to_the_most_restrictive_decision() -> None:
    registry = ToolPolicyRegistry()
    registry.replace_owner(
        "guards",
        (
            ToolPolicyRegistration(
                plugin_id="dev.ouro.confirm",
                policy_id="dangerous",
                priority=1,
                handler=lambda _call: ToolPolicyDecision(
                    action="require_approval",
                    reason="dangerous command requires confirmation",
                ),
            ),
            ToolPolicyRegistration(
                plugin_id="dev.ouro.readonly",
                policy_id="plan",
                priority=2,
                handler=lambda _call: ToolPolicyDecision(
                    action="deny",
                    reason="plan mode is read-only",
                ),
            ),
        ),
    )

    result = await registry.evaluate(_call())

    assert result.mode == "deny"
    assert result.reason == "plan mode is read-only"
    assert [item.policy_id for item in result.decisions] == ["dangerous", "plan"]
    assert result.failures == []


@pytest.mark.asyncio
async def test_tool_policy_failure_is_fail_closed_and_redacted() -> None:
    registry = ToolPolicyRegistry()

    def fail(_call):
        raise RuntimeError("token=super-secret")

    registry.replace_owner(
        "guard",
        (
            ToolPolicyRegistration(
                plugin_id="dev.ouro.guard",
                policy_id="guard",
                handler=fail,
            ),
        ),
    )

    result = await registry.evaluate(_call())

    assert result.mode == "deny"
    assert result.reason == "tool policy evaluation failed"
    assert len(result.failures) == 1
    assert result.failures[0].message == "token=***"


@pytest.mark.asyncio
async def test_tool_policy_timeout_is_fail_closed() -> None:
    registry = ToolPolicyRegistry()

    async def never(_call):
        await asyncio.Event().wait()
        return ToolPolicyDecision()

    registry.replace_owner(
        "guard",
        (
            ToolPolicyRegistration(
                plugin_id="dev.ouro.guard",
                policy_id="guard",
                handler=never,
                timeout_seconds=0.01,
            ),
        ),
    )

    result = await registry.evaluate(_call())

    assert result.mode == "deny"
    assert result.failures[0].error_type == "TimeoutError"
