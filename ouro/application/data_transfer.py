from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from ouro.application.configuration_transfer import (
    CONFIG_KINDS,
    export_configuration,
    import_configuration,
)
from ouro.application.dto import ApplicationDTO, Diagnostic
from ouro.application.session_transfer import SessionArchiveData, SessionTransferService
from ouro.skills.loader import SkillLoader
from ouro.transfer.archive import (
    DATA_KINDS,
    DataKind,
    DataTransferError,
    read_archive,
    write_archive,
)
from ouro.transfer.skills import export_skills, import_skills

TransferAction = Literal["export", "import"]


class DataTransferRequest(ApplicationDTO):
    action: TransferAction
    kind: DataKind
    path: Path
    identities: tuple[str, ...] = ()
    replace: bool = False


class DataTransferResult(ApplicationDTO):
    action: TransferAction
    kind: DataKind
    path: Path
    count: int
    restart_required: bool = False
    diagnostics: tuple[Diagnostic, ...] = ()


class DataTransferItem(ApplicationDTO):
    identity: str
    label: str


class DataTransferService:
    """Host-neutral entry point for typed configuration and data archives."""

    def __init__(self, home: Path, *, sessions: Callable[[], SessionTransferService]) -> None:
        self.home = home
        self.sessions = sessions

    def archive_kind(self, path: Path) -> DataKind:
        """Identify a validated archive without changing local data or loading a runtime."""
        return read_archive(path.expanduser()).manifest.kind

    def selection_items(self, request: DataTransferRequest) -> tuple[DataTransferItem, ...]:
        """List selectable source records without publishing data or resolving dependencies."""
        if request.kind == "sessions":
            if request.action == "import":
                archive = read_archive(request.path.expanduser(), "sessions")
                try:
                    data = SessionArchiveData.model_validate(archive.manifest.data)
                except ValidationError as exc:
                    raise DataTransferError("invalid or unsupported Session archive data") from exc
                records = [item.snapshot.session for item in data.bundle.sessions]
            else:
                records = self.sessions().sessions.list_sessions()
            return tuple(
                DataTransferItem(
                    identity=record.session_id, label=record.title or "Untitled session"
                )
                for record in records
                if record.session_kind == "primary"
            )
        if request.kind == "skills" and request.action == "export":
            return tuple(
                DataTransferItem(identity=skill.name, label=skill.name)
                for skill in SkillLoader(self.home / "skills").load_catalog().skills
            )
        return ()

    def transfer(self, request: DataTransferRequest) -> DataTransferResult:
        if request.identities and not (
            request.kind == "sessions" or (request.action == "export" and request.kind == "skills")
        ):
            raise DataTransferError("--id selects Sessions for either action or skills for export")
        if request.action == "import" and request.kind == "sessions" and request.replace:
            raise DataTransferError("Session import does not support replacement")
        path = request.path.expanduser().absolute()
        if request.action == "export":
            self._validate_export_destination(path)
        diagnostics: tuple[Diagnostic, ...] = ()
        try:
            if request.action == "export":
                if request.kind in CONFIG_KINDS:
                    archive = export_configuration(self.home, request.kind)
                    count = 1
                elif request.kind == "skills":
                    archive = export_skills(self.home, request.identities)
                    count = len(archive.manifest.data["names"])
                else:
                    archive = self.sessions().export(request.identities)
                    count = len(archive.manifest.data["bundle"]["sessions"])
                write_archive(path, archive, replace=request.replace)
            else:
                archive = read_archive(path, request.kind)
                if request.kind in CONFIG_KINDS:
                    count = import_configuration(self.home, archive, replace=request.replace)
                elif request.kind == "skills":
                    count = import_skills(self.home, archive, replace=request.replace)
                else:
                    imported = self.sessions().import_data(archive, request.identities)
                    count, diagnostics = imported.count, imported.diagnostics
        except OSError as exc:
            raise DataTransferError(f"could not {request.action} {request.kind} data") from exc
        return DataTransferResult(
            action=request.action,
            kind=request.kind,
            path=path,
            count=count,
            restart_required=request.action == "import" and request.kind != "sessions",
            diagnostics=diagnostics,
        )

    def _validate_export_destination(self, path: Path) -> None:
        destination = path.resolve()
        root = self.home.resolve()
        if any(
            destination.is_relative_to(root / directory)
            for directory in ("config", "sessions", "resources", "skills", "credentials", "locks")
        ) or destination in {root / "sessions.sqlite3", root / "layout.json"}:
            raise DataTransferError("export destination must be outside OURO's managed data paths")


__all__ = [
    "DATA_KINDS",
    "DataKind",
    "DataTransferError",
    "DataTransferItem",
    "DataTransferRequest",
    "DataTransferResult",
    "DataTransferService",
    "TransferAction",
]
