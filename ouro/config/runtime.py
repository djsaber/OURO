"""Validated snapshots of the settings consumed at runtime boundaries."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from pydantic import Field, field_validator

from ouro.config.compaction import CompactionConfig
from ouro.config.file import JsonConfigFile
from ouro.config.permissions import PermissionConfig
from ouro.config.queued_inputs import QueuedInputConfig
from ouro.config.tool_execution import ToolExecutionConfig
from ouro.core.base import OuroModel
from ouro.core.errors import ConfigError
from ouro.core.run_budget import RunBudgetPolicy


class RuntimeSettings(OuroModel):
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    tool_execution: ToolExecutionConfig = Field(default_factory=ToolExecutionConfig)
    queued_inputs: QueuedInputConfig = Field(default_factory=QueuedInputConfig)
    run_budget: RunBudgetPolicy = Field(default_factory=RunBudgetPolicy)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)

    @field_validator("permissions", "tool_execution", "queued_inputs", mode="before")
    @classmethod
    def _optional_section(cls, value: object) -> object:
        return {} if value is None else value


class RuntimeSettingsResolver:
    """Read once per file revision; publish only complete, validated snapshots."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.file = JsonConfigFile(root)
        self._lock = Lock()
        self._cached: tuple[tuple[int, int, int] | None, RuntimeSettings] | None = None

    def snapshot(self) -> RuntimeSettings:
        with self._lock:
            try:
                stat = self.file.path.stat()
                revision = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                revision = None
            except OSError as exc:
                raise ConfigError(f"could not inspect config file {self.file.path}: {exc}") from exc
            if self._cached is not None and self._cached[0] == revision:
                return self._cached[1]
            try:
                settings = RuntimeSettings.model_validate(self.file.read())
            except ValueError as exc:
                raise ConfigError(f"invalid runtime configuration: {exc}") from exc
            if self._cached is not None and settings == self._cached[1]:
                settings = self._cached[1]
            self._cached = revision, settings
            return settings
