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
