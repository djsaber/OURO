from __future__ import annotations

from datetime import datetime
from typing import Literal, Self, assert_never, cast, get_args

from pydantic import Field, model_validator

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now

ApprovalStatus = Literal["requested", "approved", "rejected", "modified", "expired", "aborted"]
ApprovalResolvedBy = Literal["user", "host_policy"]
PermissionMode = Literal["auto", "approval", "deny"]
ToolPermissionControl = Literal["host", "tool"]
SessionApprovalMode = Literal["inherit", "auto", "approval", "deny"]
SESSION_APPROVAL_MODES = cast(
    tuple[SessionApprovalMode, ...],
    get_args(SessionApprovalMode),
)

_PERMISSION_RANK: dict[PermissionMode, int] = {
    "auto": 0,
    "approval": 1,
    "deny": 2,
}


def restrict_tool_permission(
    permission_mode: PermissionMode,
    constraint: PermissionMode,
) -> PermissionMode:
    """Return the stricter of two permission decisions."""

    if _PERMISSION_RANK[constraint] > _PERMISSION_RANK[permission_mode]:
        return constraint
    return permission_mode


def resolve_tool_permission(
    permission_mode: PermissionMode,
    approval_mode: SessionApprovalMode,
    *,
    permission_control: ToolPermissionControl = "host",
    override: PermissionMode | None = None,
) -> PermissionMode:
    """Resolve permission according to the authority declared by the tool definition."""

    match permission_control:
        case "tool":
            return permission_mode
        case "host":
            configured_mode = override or permission_mode
        case _ as invalid_control:
            assert_never(invalid_control)

    if configured_mode == "deny":
        return "deny"

    match approval_mode:
        case "inherit":
            return configured_mode
        case "auto":
            return "auto"
        case "approval":
            return "approval"
        case "deny":
            return "deny"
        case _ as invalid_mode:
            assert_never(invalid_mode)


class ApprovalRecord(OuroModel):
    schema_version: int = SCHEMA_VERSION
    approval_id: str = Field(default_factory=lambda: new_id("apv"))
    session_id: str
    run_id: str
    tool_call_id: str
    execution_contract_hash: str | None = None
    status: ApprovalStatus = "requested"
    requested_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
    resolved_by: ApprovalResolvedBy | None = None
    resolver_id: str | None = None
    reason: str | None = None
    modified_arguments: dict[str, object] | None = None
    metadata: Metadata = MetadataField

    @model_validator(mode="after")
    def validate_resolution_provenance(self) -> Self:
        if self.resolved_by == "host_policy" and not self.resolver_id:
            raise ValueError("host policy approval requires resolver_id")
        if self.resolved_by != "host_policy" and self.resolver_id is not None:
            raise ValueError("approval resolver_id requires host policy provenance")
        return self
