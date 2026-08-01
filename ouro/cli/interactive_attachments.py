from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ouro.application.resource_models import ResourceView


@dataclass(slots=True)
class InteractiveAttachmentDraft:
    """Session-scoped resources selected for the next terminal submission."""

    _session_id: str | None = None
    _resources: list[ResourceView] = field(default_factory=list)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def count(self) -> int:
        return len(self._resources)

    def add(self, session_id: str, resource: ResourceView) -> tuple[int, bool]:
        self.retain_for_session(session_id)
        for index, existing in enumerate(self._resources, start=1):
            if existing.resource_id == resource.resource_id:
                return index, False
        self._resources.append(resource)
        return len(self._resources), True

    def resources(self, session_id: str | None) -> tuple[ResourceView, ...]:
        if session_id is None or session_id != self._session_id:
            return ()
        return tuple(self._resources)

    def resource_refs(self, session_id: str | None) -> tuple[str, ...]:
        return tuple(resource.resource_id for resource in self.resources(session_id))

    def detach(self, session_id: str | None, position: int) -> ResourceView | None:
        if session_id is None or session_id != self._session_id:
            return None
        if position < 1 or position > len(self._resources):
            return None
        return self._resources.pop(position - 1)

    def retain_for_session(self, session_id: str | None) -> None:
        if session_id == self._session_id:
            return
        self.clear()
        self._session_id = session_id

    def clear(self) -> None:
        self._resources.clear()
        self._session_id = None


def attachment_path(argument: str) -> Path:
    """Normalize one terminal path while preserving spaces inside the path."""

    value = argument.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return Path(value).expanduser()


def format_attachment(resource: ResourceView, position: int) -> str:
    return (
        f"[{position}] {resource.source.name} "
        f"({resource.media_type}, {_format_bytes(resource.size)})"
    )


def _format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


__all__ = [
    "InteractiveAttachmentDraft",
    "attachment_path",
    "format_attachment",
]
