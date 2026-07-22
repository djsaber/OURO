from __future__ import annotations

import pytest

from ouro.tools import Tool, ToolDefinition, ToolInvocation, builtin_tools
from ouro.tools.builtin_shell import detect_command_shell
from ouro.tools.executor import ToolExecutor
from ouro.tools.registry import ToolRegistry


def _command_tool_name() -> str:
    shell = detect_command_shell()
    assert shell is not None
    return shell.tool_name


def test_tool_controlled_permission_rejects_approval_mode() -> None:
    with pytest.raises(ValueError, match="tool-controlled permission must be auto or deny"):
        ToolDefinition(
            name="ControlHarness",
            description="Control the harness.",
            permission_mode="approval",
            permission_control="tool",
        )


@pytest.mark.asyncio
async def test_permission_override_changes_mode_to_approval() -> None:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    command_tool_name = _command_tool_name()
    executor = ToolExecutor(registry, permission_overrides={command_tool_name: "approval"})

    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name=command_tool_name,
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


@pytest.mark.parametrize("approval_mode", ["auto", "approval"])
def test_session_mode_cannot_override_denied_tool_permission(approval_mode) -> None:
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

    outcome = executor.preflight(invocation, approval_mode=approval_mode)

    assert outcome.status == "completed"
    assert outcome.invocation.permission_mode == "deny"
    assert outcome.result is not None
    assert outcome.result.structured["error_type"] == "ToolPermissionDenied"


@pytest.mark.parametrize("approval_mode", ["inherit", "auto", "approval", "deny"])
@pytest.mark.parametrize(
    ("permission_mode", "override", "policy_mode", "expected_status", "expected_mode"),
    [
        ("auto", "deny", "deny", "completed", "deny"),
        ("auto", "deny", "approval", "approval_required", "approval"),
        ("deny", "auto", "ignore", "completed", "deny"),
    ],
)
def test_tool_controlled_permission_bypasses_host_mode_but_not_tool_policy(
    approval_mode,
    permission_mode,
    override,
    policy_mode,
    expected_status,
    expected_mode,
) -> None:
    async def execute(_invocation):
        return "unused"

    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="ControlHarness",
                description="Control the harness.",
                permission_mode=permission_mode,
                permission_control="tool",
            ),
            handler=execute,
        )
    )
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="ControlHarness",
    )
    executor = ToolExecutor(registry, permission_overrides={"ControlHarness": override})

    outcome = executor.preflight(
        invocation,
        approval_mode=approval_mode,
        policy_mode=policy_mode,
        policy_reason="extension policy governs every tool",
    )

    assert outcome.status == expected_status
    assert outcome.invocation.permission_control == "tool"
    assert outcome.invocation.permission_mode == expected_mode
    if expected_mode == "deny":
        assert outcome.result is not None
        assert outcome.result.structured["error_type"] == "ToolPermissionDenied"


@pytest.mark.parametrize(
    "source",
    ["builtin", "official_plugin", "third_party_plugin", "ai_generated_plugin", "mcp"],
)
@pytest.mark.parametrize("permission_mode", ["auto", "approval", "deny"])
def test_session_deny_mode_blocks_every_tool_source(source, permission_mode) -> None:
    async def execute(_invocation):
        return "unused"

    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="extension__run",
                description="Run an extension tool.",
                source=source,
                permission_mode=permission_mode,
            ),
            handler=execute,
        )
    )
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="extension__run",
    )

    outcome = ToolExecutor(registry).preflight(invocation, approval_mode="deny")

    assert outcome.status == "completed"
    assert outcome.invocation.source == source
    assert outcome.invocation.permission_mode == "deny"
    assert outcome.result is not None
    assert outcome.result.structured["error_type"] == "ToolPermissionDenied"


@pytest.mark.parametrize(
    "source",
    ["official_plugin", "third_party_plugin", "ai_generated_plugin", "mcp"],
)
@pytest.mark.parametrize(
    ("approval_mode", "expected_status", "expected_permission"),
    [
        ("inherit", "approval_required", "approval"),
        ("auto", "ready", "auto"),
        ("approval", "approval_required", "approval"),
        ("deny", "completed", "deny"),
    ],
)
def test_extension_tools_use_shared_overrides_and_session_approval_modes(
    source,
    approval_mode,
    expected_status,
    expected_permission,
) -> None:
    async def execute(_invocation):
        return "unused"

    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="extension__run",
                description="Run an extension tool.",
                source=source,
                permission_mode="deny",
            ),
            handler=execute,
        )
    )
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="extension__run",
    )

    executor = ToolExecutor(registry, permission_overrides={"extension__run": "approval"})
    outcome = executor.preflight(invocation, approval_mode=approval_mode)

    assert outcome.status == expected_status
    assert outcome.invocation.source == source
    assert outcome.invocation.permission_mode == expected_permission


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


def test_delegated_permission_floor_cannot_be_relaxed_by_current_configuration() -> None:
    registry = ToolRegistry()
    for tool in builtin_tools():
        registry.register(tool)
    executor = ToolExecutor(
        registry,
        permission_overrides={"Read": "auto"},
        permission_floors={"Read": "approval"},
    )
    invocation = ToolInvocation(
        session_id="child",
        run_id="child_run",
        tool_name="Read",
        arguments={"path": "unused.txt"},
    )

    outcome = executor.preflight(invocation, approval_mode="auto")

    assert outcome.status == "approval_required"
    assert outcome.invocation.permission_mode == "approval"


def test_persisted_plugin_tool_requires_the_same_execution_contract() -> None:
    async def execute(_invocation):
        return "unused"

    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="example__run",
                description="Run the example plugin.",
                metadata={
                    "plugin_id": "dev.ouro.example",
                    "plugin_class": "user",
                    "release_id": "a" * 64,
                    "package_digest": "a" * 64,
                    "contract_hash": "b" * 64,
                    "execution_contract_hash": "c" * 64,
                },
            ),
            handler=execute,
        )
    )
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="example__run",
        plugin_id="dev.ouro.example",
        plugin_class="user",
        release_id="a" * 64,
        package_digest="a" * 64,
        contract_hash="b" * 64,
        execution_contract_hash="d" * 64,
    )

    outcome = ToolExecutor(registry).preflight(invocation)

    assert outcome.status == "completed"
    assert outcome.result is not None
    assert outcome.result.structured["error_type"] == "ToolExecutionContractMismatch"


def test_legacy_user_plugin_tool_contract_remains_recoverable() -> None:
    async def execute(_invocation):
        return "unused"

    registry = ToolRegistry()
    registry.register(
        Tool(
            definition=ToolDefinition(
                name="example__run",
                description="Run the example plugin.",
                metadata={
                    "plugin_id": "dev.ouro.example",
                    "plugin_class": "user",
                    "release_id": "a" * 64,
                    "package_digest": "a" * 64,
                    "contract_hash": "b" * 64,
                    "execution_contract_hash": "c" * 64,
                },
            ),
            handler=execute,
        )
    )
    invocation = ToolInvocation(
        session_id="s1",
        run_id="r1",
        tool_name="example__run",
        plugin_id="dev.ouro.example",
        package_digest="a" * 64,
        contract_hash="b" * 64,
        execution_contract_hash="c" * 64,
    )

    assert ToolExecutor(registry).preflight(invocation).status == "ready"
