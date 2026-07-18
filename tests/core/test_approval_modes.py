from __future__ import annotations

import pytest

from ouro.core.approvals import resolve_tool_permission


@pytest.mark.parametrize(
    ("approval_mode", "tool_permission", "expected"),
    [
        ("inherit", "auto", "auto"),
        ("inherit", "approval", "approval"),
        ("inherit", "deny", "deny"),
        ("auto", "auto", "auto"),
        ("auto", "approval", "auto"),
        ("auto", "deny", "auto"),
        ("approval", "auto", "approval"),
        ("approval", "approval", "approval"),
        ("approval", "deny", "approval"),
        ("deny", "auto", "deny"),
        ("deny", "approval", "deny"),
        ("deny", "deny", "deny"),
    ],
)
def test_approval_mode_resolves_every_tool_permission(
    approval_mode: str,
    tool_permission: str,
    expected: str,
) -> None:
    assert resolve_tool_permission(tool_permission, approval_mode) == expected


@pytest.mark.parametrize("approval_mode", ["inherit", "auto", "approval", "deny"])
@pytest.mark.parametrize(
    ("permission_mode", "override", "expected"),
    [
        ("auto", "deny", "auto"),
        ("deny", "auto", "deny"),
    ],
)
def test_tool_controlled_permission_ignores_host_strategy_and_override(
    approval_mode: str,
    permission_mode: str,
    override: str,
    expected: str,
) -> None:
    assert (
        resolve_tool_permission(
            permission_mode,
            approval_mode,
            permission_control="tool",
            override=override,
        )
        == expected
    )
