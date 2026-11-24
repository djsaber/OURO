"""Labels and context displayed alongside user input."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ouro.core.input_content import InputFile


@dataclass(frozen=True, kw_only=True)
class ContextElement:
    """A removable composer context with a persistent label and an optional preview."""

    label: str
    preview: str


def reference_label(name: str) -> str:
    return f"@{name}"


def reference_labels(names: Iterable[str]) -> str:
    return " ".join(reference_label(name) for name in names)


def file_count_label(items: tuple[InputFile, ...], resource_refs: tuple[str, ...] = ()) -> str:
    uploads = sum(item.kind == "upload" for item in items) if items else len(resource_refs)
    references = sum(item.kind == "reference" for item in items)
    return " · ".join(
        f"{count} {label}{'s' if count != 1 else ''}"
        for count, label in ((uploads, "attachment"), (references, "reference"))
        if count
    )
