"""Construct SDK contexts from explicit host capabilities."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from ouro.core.redaction import SensitiveValueRedactor
from ouro.plugins.configuration.values import freeze_configuration
from ouro.plugins.credentials import PluginCredentialBinding, resolve_credential_binding
from ouro.plugins.lifecycle.candidates import PluginRuntimeCandidate
from ouro.plugins.models import PluginDiagnostic, PluginGenerationRecord
from ouro.plugins.resources import OwnedTaskGroup, ScopedPluginDataStore
from ouro.sdk.plugins import v5 as sdk


class BoundCredentialResolver:
    def __init__(
        self,
        bindings: dict[str, PluginCredentialBinding],
        resolve_reference: Callable[[str], str] | None,
        redactor: SensitiveValueRedactor,
    ) -> None:
        self._bindings = dict(bindings)
        self._resolve_reference = resolve_reference
        self._redactor = redactor

    def resolve(self, slot: str) -> str:
        try:
            reference = self._bindings[slot]
        except KeyError as exc:
            raise PermissionError(f"credential slot is not bound: {slot}") from exc
        try:
            value = resolve_credential_binding(reference, self._resolve_reference)
        except Exception:
            # Resolver failures may contain transport payloads or secret material.
            raise RuntimeError(f"credential slot cannot be resolved: {slot}") from None
        if not value:
            raise RuntimeError(f"credential slot cannot be resolved: {slot}")
        self._redactor.add(value)
        return value


class RedactedPluginLogger:
    def __init__(self, plugin_id: str, redactor: SensitiveValueRedactor) -> None:
        self._logger = logging.getLogger(f"ouro.plugin.{plugin_id}")
        self._redactor = redactor

    def debug(self, message: str, *args: object) -> None:
        self._log(logging.DEBUG, message, args)

    def info(self, message: str, *args: object) -> None:
        self._log(logging.INFO, message, args)

    def warning(self, message: str, *args: object) -> None:
        self._log(logging.WARNING, message, args)

    def error(self, message: str, *args: object) -> None:
        self._log(logging.ERROR, message, args)

    def _log(self, level: int, message: str, args: tuple[object, ...]) -> None:
        try:
            rendered = message % args if args else message
        except TypeError, ValueError:
            rendered = " ".join((message, *(str(item) for item in args)))
        self._logger.log(level, self._redactor.redact(rendered))


class PluginDiagnosticEmitter:
    def __init__(
        self,
        candidate: PluginRuntimeCandidate,
        generation: PluginGenerationRecord,
        sink: list[PluginDiagnostic],
        redactor: SensitiveValueRedactor,
    ) -> None:
        self.candidate = candidate
        self.generation = generation
        self.sink = sink
        self.redactor = redactor

    def emit(
        self,
        code: str,
        message: str,
        *,
        severity: str = "warning",
    ) -> None:
        normalized_severity = severity if severity in {"info", "warning", "error"} else "warning"
        self.sink.append(
            PluginDiagnostic(
                code=code,
                phase="runtime",
                severity=normalized_severity,  # type: ignore[arg-type]
                message=self.redactor.redact(message),
                plugin_id=self.candidate.plugin_id,
                version=self.candidate.version,
                plugin_class=self.candidate.plugin_class,
                release_id=self.candidate.release_id,
                package_digest=self.candidate.package_digest,
                builtin_contract_hash=self.candidate.builtin_contract_hash,
                generation_id=self.generation.generation_id,
            )
        )


def build_plugin_context(
    candidate: PluginRuntimeCandidate,
    generation: PluginGenerationRecord,
    *,
    data_root: Path,
    configuration: dict[str, object],
    credential_bindings: dict[str, PluginCredentialBinding],
    credential_resolver: Callable[[str], str] | None,
    tasks: OwnedTaskGroup,
    diagnostics: list[PluginDiagnostic],
    redactor: SensitiveValueRedactor,
    data_store: ScopedPluginDataStore | None = None,
    artifact_root: Path | None = None,
    environment: sdk.PluginEnvironment | None = None,
    services: sdk.PluginServices | None = None,
    processes: sdk.PluginProcesses | None = None,
    models: sdk.PluginModels | None = None,
    tools: sdk.RunTools | None = None,
    tool_groups: sdk.PluginToolGroups | None = None,
    scheduler: sdk.PluginScheduler | None = None,
) -> sdk.PluginContext:
    return sdk.PluginContext(
        identity=sdk.PluginIdentity(
            plugin_id=candidate.plugin_id,
            version=candidate.version,
            plugin_class=candidate.plugin_class,
            release_id=candidate.release_id,
            package_digest=candidate.package_digest,
            builtin_contract_hash=candidate.builtin_contract_hash,
            generation_id=generation.generation_id,
        ),
        provenance=sdk.PluginProvenanceSummary(
            source_kind=candidate.source_kind,
            public_source=candidate.public_source,
        ),
        configuration=freeze_configuration(configuration),
        data=data_store or ScopedPluginDataStore(data_root / candidate.plugin_id),
        credentials=BoundCredentialResolver(
            credential_bindings,
            credential_resolver,
            redactor,
        ),
        tasks=tasks,
        logger=RedactedPluginLogger(candidate.plugin_id, redactor),
        diagnostics=PluginDiagnosticEmitter(candidate, generation, diagnostics, redactor),
        permissions=frozenset(request.id for request in candidate.manifest.permissions),
        artifact_root=(
            str((artifact_root / candidate.plugin_id).resolve(strict=False))
            if artifact_root is not None
            else None
        ),
        environment=environment,
        services=services,
        processes=processes,
        models=models,
        tools=tools,
        agents=sdk.RunAgents(tools) if tools is not None else None,
        tool_groups=tool_groups,
        scheduler=scheduler,
    )
