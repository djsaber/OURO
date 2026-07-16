from __future__ import annotations

import pytest

from ouro.tools import ToolInvocation, builtin_tools
from ouro.tools.executor import ToolExecutor
from ouro.tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_permission_override_changes_mode_to_approval() -> None:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    executor = ToolExecutor(registry, permission_overrides={"Bash": "approval"})

    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="Bash",
        arguments={"command": "echo hi"},
    )
    outcome = await executor.execute(invocation)

    assert outcome.status == "approval_required"
    assert outcome.invocation.permission_mode == "approval"


@pytest.mark.asyncio
async def test_permission_override_changes_mode_to_auto(tmp_path) -> None:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    executor = ToolExecutor(registry, permission_overrides={"Write": "auto"})

    target = tmp_path / "auto.txt"
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="Write",
        arguments={"path": str(target), "content": "auto-written"},
    )
    outcome = await executor.execute(invocation)

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.is_error is False
    assert target.read_text(encoding="utf-8") == "auto-written"


def test_session_auto_mode_overrides_protected_tool_policy() -> None:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    executor = ToolExecutor(registry)
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="Write",
        arguments={"path": "unused.txt", "content": "content"},
    )

    outcome = executor.preflight(invocation, approval_mode="auto")

    assert outcome.status == "ready"
    assert outcome.invocation.permission_mode == "auto"


def test_session_approval_mode_overrides_automatic_tool_policy() -> None:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    executor = ToolExecutor(registry, permission_overrides={"Read": "auto"})
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="Read",
        arguments={"path": "unused.txt"},
    )

    outcome = executor.preflight(invocation, approval_mode="approval")

    assert outcome.status == "approval_required"
    assert outcome.invocation.permission_mode == "approval"


def test_deny_policy_cannot_be_overridden_by_session_auto_mode() -> None:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    executor = ToolExecutor(registry, permission_overrides={"Write": "deny"})
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="Write",
        arguments={"path": "unused.txt", "content": "content"},
    )

    outcome = executor.preflight(invocation, approval_mode="auto")

    assert outcome.status == "completed"
    assert outcome.invocation.permission_mode == "deny"
    assert outcome.result is not None
    assert outcome.result.structured["error_type"] == "ToolPermissionDenied"


def test_extension_policy_can_raise_auto_tool_to_approval() -> None:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    executor = ToolExecutor(registry, permission_overrides={"Read": "auto"})
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="Read",
        arguments={"path": "unused.txt"},
    )

    outcome = executor.preflight(
        invocation,
        approval_mode="auto",
        policy_mode="approval",
        policy_reason="review protected path",
    )

    assert outcome.status == "approval_required"
    assert outcome.invocation.permission_mode == "approval"


def test_extension_policy_deny_cannot_be_lowered_by_session_mode() -> None:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    executor = ToolExecutor(registry)
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="Read",
        arguments={"path": "secret.txt"},
    )

    outcome = executor.preflight(
        invocation,
        approval_mode="auto",
        policy_mode="deny",
        policy_reason="path is protected",
    )

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.structured["error_type"] == "ToolPermissionDenied"
    assert "path is protected" in outcome.result.content
