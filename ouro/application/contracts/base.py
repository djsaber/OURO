from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field

from ouro.core.base import Metadata, MetadataField, OuroModel, utc_now

DiagnosticSeverity = Literal["info", "warning", "error"]


class ApplicationDTO(OuroModel):
    """Base class for immutable values exposed to OURO hosts."""

    model_config = ConfigDict(frozen=True)


class Diagnostic(ApplicationDTO):
    code: str
    severity: DiagnosticSeverity
    component: str
    message: str
    session_id: str | None = None
    run_id: str | None = None
    resource_id: str | None = None
    server_id: str | None = None
    plugin_id: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)
    recoverable: bool = False
    suggested_action: str | None = None
    metadata: Metadata = MetadataField


class ApplicationStartupReport(ApplicationDTO):
    """Host-neutral diagnostics produced while application resources start."""

    diagnostics: tuple[Diagnostic, ...] = ()
