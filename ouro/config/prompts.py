"""Persistent personal prompts and their deterministic runtime composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from ouro.config.file import JsonConfigFile, atomic_config_update
from ouro.core.base import OuroModel, new_id
from ouro.core.context import compose_system_prompt
from ouro.core.errors import ConfigError
from ouro.prompts import DEFAULT_SYSTEM_PROMPT

PromptKind = Literal["replacement", "addition"]


class PromptDraft(OuroModel):
    """User-editable prompt fields; identity belongs to the persistence boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = Field(default="", max_length=500)
    text: str = Field(min_length=1)
    enabled: bool = Field(default=False, strict=True)

    @field_validator("text")
    @classmethod
    def require_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt text must not be blank")
        return value


class PromptEntry(PromptDraft):
    id: str = Field(pattern=r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$", max_length=64)


class PromptConfig(OuroModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(default=0, ge=0, strict=True)
    replacements: tuple[PromptEntry, ...] = ()
    additions: tuple[PromptEntry, ...] = ()

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        for entries in (self.replacements, self.additions):
            ids = [entry.id for entry in entries]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate prompt id within a prompt kind")
        if sum(entry.enabled for entry in self.replacements) > 1:
            raise ValueError("at most one replacement prompt may be enabled")
        return self

    def entries(self, kind: PromptKind) -> tuple[PromptEntry, ...]:
        if kind == "replacement":
            return self.replacements
        if kind == "addition":
            return self.additions
        raise ConfigError(f"unknown prompt kind: {kind}")

    @property
    def system_prompt(self) -> str | None:
        return next((entry.text for entry in self.replacements if entry.enabled), None)

    @property
    def additional_system_prompt(self) -> str:
        return compose_system_prompt("", *(entry.text for entry in self.additions if entry.enabled))

    def resolve_system_prompt(self, override: str | None = None) -> str:
        if override is not None:
            return override
        return self.system_prompt if self.system_prompt is not None else DEFAULT_SYSTEM_PROMPT


@dataclass(frozen=True)
class PromptConfigSnapshot:
    config: PromptConfig
    config_path: Path


class PromptConfigManager:
    def __init__(self, root: str | Path | None = None) -> None:
        self._file = JsonConfigFile(root, name="prompts.json")
        self.root = self._file.root
        self.config_path = self._file.path

    def snapshot(self) -> PromptConfigSnapshot:
        raw = self._file.read()
        if raw.keys() - {"prompts"}:
            raise ConfigError("prompts.json only supports the prompts section")
        try:
            config = PromptConfig.model_validate(raw.get("prompts", {}))
        except ValidationError as exc:
            # Diagnostics must identify the invalid field without copying prompt bodies.
            errors = exc.errors(include_input=False, include_url=False, include_context=False)
            raise ConfigError(f"invalid prompts config: {errors}") from exc
        return PromptConfigSnapshot(config=config, config_path=self.config_path)

    @atomic_config_update
    def save_entry(
        self,
        kind: PromptKind,
        draft: PromptDraft,
        *,
        entry_id: str | None = None,
        expected_revision: int,
    ) -> PromptConfigSnapshot:
        current = self._require_revision(expected_revision)
        entries = list(current.entries(kind))
        # Allocate identity only on creation, never while reading or rendering drafts.
        values = PromptDraft.model_validate(draft.model_dump())
        entry = PromptEntry(
            id=new_id("prompt") if entry_id is None else entry_id, **values.model_dump()
        )
        if entry_id is None:
            entries.append(entry)
        else:
            index = self._entry_index(entries, entry_id)
            entries[index] = entry
        # Selecting a replacement is one atomic switch, never a transient double selection.
        if kind == "replacement" and entry.enabled:
            entries = [
                item.model_copy(update={"enabled": False}) if item.id != entry.id else item
                for item in entries
            ]
        return self._save(current, kind, entries)

    @atomic_config_update
    def remove_entry(
        self, kind: PromptKind, entry_id: str, *, expected_revision: int
    ) -> PromptConfigSnapshot:
        current = self._require_revision(expected_revision)
        entries = list(current.entries(kind))
        entries.pop(self._entry_index(entries, entry_id))
        return self._save(current, kind, entries)

    @atomic_config_update
    def set_enabled(
        self, kind: PromptKind, entry_id: str, enabled: bool, *, expected_revision: int
    ) -> PromptConfigSnapshot:
        current = self._require_revision(expected_revision)
        entries = list(current.entries(kind))
        index = self._entry_index(entries, entry_id)
        entries = [
            item.model_copy(update={"enabled": enabled})
            if i == index
            else item.model_copy(update={"enabled": False})
            if kind == "replacement" and enabled
            else item
            for i, item in enumerate(entries)
        ]
        return self._save(current, kind, entries)

    @atomic_config_update
    def move_entry(
        self, kind: PromptKind, entry_id: str, position: int, *, expected_revision: int
    ) -> PromptConfigSnapshot:
        current = self._require_revision(expected_revision)
        entries = list(current.entries(kind))
        index = self._entry_index(entries, entry_id)
        if not 1 <= position <= len(entries):
            raise ConfigError(f"prompt position must be between 1 and {len(entries)}")
        entries.insert(position - 1, entries.pop(index))
        return self._save(current, kind, entries)

    def _require_revision(self, expected_revision: int) -> PromptConfig:
        current = self.snapshot().config
        if current.revision != expected_revision:
            raise ConfigError(
                f"prompts revision conflict: expected {expected_revision}, found {current.revision}"
            )
        return current

    @staticmethod
    def _entry_index(entries: list[PromptEntry], entry_id: str) -> int:
        for index, entry in enumerate(entries):
            if entry.id == entry_id:
                return index
        raise ConfigError(f"prompt not found: {entry_id}")

    def _save(
        self, current: PromptConfig, kind: PromptKind, entries: list[PromptEntry]
    ) -> PromptConfigSnapshot:
        field = "replacements" if kind == "replacement" else "additions"
        raw = {
            **current.model_dump(),
            field: [entry.model_dump() for entry in entries],
            "revision": current.revision + 1,
        }
        try:
            validated = PromptConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"invalid prompts config: {exc}") from exc
        self._file.write({"prompts": validated.model_dump(mode="json")})
        return PromptConfigSnapshot(config=validated, config_path=self.config_path)
