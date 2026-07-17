from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from ouro.core.base import SCHEMA_VERSION, Metadata, MetadataField, OuroModel, new_id, utc_now

ApprovalStatus = Literal["requested", "approved", "rejected", "modified", "expired", "aborted"]
SessionApprovalMode = Literal["inherit", "auto", "approval"]
SESSION_APPROVAL_MODES: tuple[SessionApprovalMode, ...] = ("inherit", "auto", "approval")


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
