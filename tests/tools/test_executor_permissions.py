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
