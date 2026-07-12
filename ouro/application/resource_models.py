from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ouro.application.dto import ApplicationDTO, Diagnostic
from ouro.core.resources import ResourceRecord, ResourceSource


class ResourceView(ApplicationDTO):
    resource_id: str
    digest: str
    media_type: str
    size: int
    source: ResourceSource
    created_at: datetime

    @classmethod
    def from_record(cls, record: ResourceRecord) -> ResourceView:
        return cls.model_validate(record.model_dump(exclude={"schema_version"}))


class ResourceMutationResult(ApplicationDTO):
    resource: ResourceView
    created: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()


class ResourceListResult(ApplicationDTO):
    session_id: str
    resources: tuple[ResourceView, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


class ResourceExportResult(ApplicationDTO):
    resource: ResourceView
    destination: Path
    diagnostics: tuple[Diagnostic, ...] = ()


class ResourceRemoveResult(ApplicationDTO):
    session_id: str
    resource_id: str
    content_collected: bool
    diagnostics: tuple[Diagnostic, ...] = ()
