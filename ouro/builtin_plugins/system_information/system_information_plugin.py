"""Builtin request-time system information context."""

from __future__ import annotations

import locale
import os
import platform
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from ouro import __version__
from ouro.sdk.plugins.v1 import (
    ContextCall,
    ContextContribution,
    ContextFragment,
    ContextOutput,
    ContributionSet,
    PluginContext,
)

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class RuntimeFacts:
    platform: str
    architecture: str
    runtime: str
    shell: str
    preferred_encoding: str
    path_style: str

    @classmethod
    def collect(cls) -> RuntimeFacts:
        system = platform.system() or sys.platform
        release = platform.release()
        return cls(
            platform=" ".join(value for value in (system, release) if value),
            architecture=platform.machine() or "unknown",
            runtime=(
                f"OURO {__version__}; {platform.python_implementation()} "
                f"{platform.python_version()}"
            ),
            shell=os.environ.get("COMSPEC") or os.environ.get("SHELL") or "unknown",
            preferred_encoding=locale.getpreferredencoding(False),
            path_style=("Windows" if os.name == "nt" else "POSIX"),
        )


class SystemInformationPlugin:
    def __init__(
        self,
        *,
        facts: RuntimeFacts | None = None,
        clock: Clock | None = None,
        fallback_working_directory: str | Path | None = None,
    ) -> None:
        self._facts = facts or RuntimeFacts.collect()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._fallback_working_directory = (
            str(Path.cwd().resolve())
            if fallback_working_directory is None
            else str(fallback_working_directory)
        )

    async def prepare(self) -> ContributionSet:
        return ContributionSet(
            context_providers=[
                ContextContribution(
                    id="runtime-facts",
                    handler=self.system_information,
                    priority=10_000,
                    timeout_seconds=5,
                    failure_mode="omit",
                )
            ]
        )

    async def start(self) -> None:
        return None

    async def stop(self, reason, deadline) -> None:
        del reason, deadline

    def system_information(self, call: ContextCall) -> ContextOutput:
        return ContextOutput(
            fragments=(
                ContextFragment(
                    text=self._render(call, self._clock()),
                    target="system_prompt",
                ),
            )
        )

    def _render(self, call: ContextCall, current: datetime) -> str:
        timezone = _timezone_description(current)
        working_directory = call.workspace or self._fallback_working_directory
        values = (
            ("System platform", self._facts.platform),
            ("Architecture", self._facts.architecture),
            ("Runtime environment", self._facts.runtime),
            ("Current date", current.date().isoformat()),
            ("Time zone", timezone),
            ("Workspace", working_directory),
            ("Shell", self._facts.shell),
            ("Path style", self._facts.path_style),
            ("Preferred encoding", self._facts.preferred_encoding),
        )
        body = "\n".join(f"{label}: {escape(value, quote=False)}" for label, value in values)
        return f"<ouro-system-information>\n{body}\n</ouro-system-information>"


def _timezone_description(current: datetime) -> str:
    name = current.tzname() or "local"
    offset = current.utcoffset()
    if offset is None:
        return name
    sign = "+" if offset >= timedelta() else "-"
    total_minutes = abs(int(offset.total_seconds() // 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{name} (UTC{sign}{hours:02d}:{minutes:02d})"


def create_plugin(context: PluginContext) -> SystemInformationPlugin:
    del context
    return SystemInformationPlugin()


__all__ = [
    "RuntimeFacts",
    "SystemInformationPlugin",
    "create_plugin",
]
