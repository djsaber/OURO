from __future__ import annotations

from datetime import datetime
from typing import Literal, assert_never, cast, get_args

from pydantic import Field

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now

ApprovalStatus = Literal["requested", "approved", "rejected", "modified", "expired", "aborted"]
PermissionMode = Literal["auto", "approval", "deny"]
SessionApprovalMode = Literal["inherit", "auto", "approval", "deny"]
SESSION_APPROVAL_MODES = cast(
    tuple[SessionApprovalMode, ...],
    get_args(SessionApprovalMode),
)


def resolve_tool_permission(
    permission_mode: PermissionMode,
    approval_mode: SessionApprovalMode,
) -> PermissionMode:
    """Apply a session approval strategy to an already configured tool permission."""

    match approval_mode:
        case "inherit":
            return permission_mode
        case "auto":
            return "auto"
        case "approval":
            return "approval"
        case "deny":
            return "deny"
        case _ as unreachable:
            assert_never(unreachable)


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
    reason: str | None = None
    modified_arguments: dict[str, object] | None = None
    metadata: Metadata = MetadataField
