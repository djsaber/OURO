from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext, redirect_stdout
from io import StringIO
from pathlib import Path

from ouro.application.plugin_configuration import (
    PluginConfigurationConflictError,
    PluginConfigurationValidationError,
)
from ouro.application.plugin_configuration_models import (
    PluginConfigurationCandidate,
    PluginConfigurationPreview,
    PluginConfigurationResult,
    parse_credential_bindings,
)
from ouro.application.plugin_models import PluginActionResult
from ouro.application.plugins import PluginService
from ouro.core.base import OuroModel
from ouro.core.errors import OuroError
from ouro.core.redaction import redact_text
from ouro.local_application import (
    build_local_plugin_maintenance_service,
    build_local_plugin_service,
    open_local_plugins,
    resolve_local_home,
)
from ouro.local_permissions import reject_link, restrict_to_user


def run_plugin_command(args) -> int:
    try:
        return _run_plugin_command(args)
    except PluginConfigurationValidationError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "error": {
                            "code": "invalid_configuration",
                            "issues": [issue.model_dump() for issue in exc.issues],
                        }
                    }
                )
            )
            return 1
        raise OuroError(str(exc)) from None
    except PluginConfigurationConflictError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "error": {
                            "code": "configuration_conflict",
                            "plugin_id": exc.plugin_id,
                            "package_digest": exc.package_digest,
                            "current_revision_id": exc.current_revision_id,
                        }
                    }
                )
            )
            return 1
        raise OuroError(str(exc)) from None
    except OuroError:
        raise
    except Exception as exc:
        raise OuroError(redact_text(str(exc))) from exc


def _run_plugin_command(args) -> int:
    command = args.plugin_command
    home = resolve_local_home(args.home)
    if command == "purge-data":
        service = build_local_plugin_maintenance_service(home, session_backend=args.session_backend)
        return _emit(service.purge_data(args.plugin_id), json_mode=args.json)
    if command == "maintain":
        service = build_local_plugin_maintenance_service(home, session_backend=args.session_backend)
        result = asyncio.run(
            service.maintain_data(args.plugin_id, args.execute, timeout_seconds=args.timeout)
        )
        _emit(result, json_mode=args.json)
        return 0 if result.status == "succeeded" else 1
    if command == "gc":
        service = build_local_plugin_maintenance_service(home, session_backend=args.session_backend)
        removed = service.gc()
        if args.json:
            print(json.dumps({"removed": removed}, ensure_ascii=False))
        else:
            print(f"Removed {len(removed)} unreferenced plugin paths.")
        return 0
    service = build_local_plugin_service(home)
    if command == "scaffold":
        return _emit(
            service.scaffold(
                args.destination,
                plugin_id=args.plugin_id,
                name=args.name,
                namespace=args.namespace,
            ),
            json_mode=args.json,
        )
    if command == "validate":
        validation = service.validate(args.source, ref=args.ref, subdirectory=args.subdirectory)
        return _emit(validation, json_mode=args.json)
    if command == "import-report":
        return _emit(service.import_compatibility(args.source), json_mode=args.json)
    if command == "list":
        return _emit_many(service.list(), json_mode=args.json)
    if command == "show":
        return _emit(service.show(args.plugin_id), json_mode=args.json)
    if command == "configuration":
        view = service.configuration(args.plugin_id, package_digest=args.digest)
        if args.export_path:
            export_path = Path(args.export_path)
            reject_link(export_path)
            export_path.touch(mode=0o600, exist_ok=True)
            restrict_to_user(export_path)
            export_path.write_text(
                view.candidate().model_dump_json(indent=2, context={"include_secrets": True})
                + "\n",
                encoding="utf-8",
            )
        return _emit(view, json_mode=args.json)
    if command in {"commands", "run-command"}:
        return asyncio.run(_plugin_contributed_command(home, args))
    if command == "install":
        install_result = service.install(
            args.source,
            ref=args.ref,
            subdirectory=args.subdirectory,
            request_id=args.request_id,
            preview=args.preview,
        )
        return _emit(install_result, json_mode=args.json)
    if command == "configure":
        candidate = _configuration_candidate(service, args)
        preview = service.preview_configuration(candidate)
        if args.preview or not preview.valid:
            _emit(preview, json_mode=args.json)
            return 0 if preview.valid else 1
        return asyncio.run(
            _configure_command(
                home,
                candidate,
                needs_runtime=preview.scope == "current_host",
                request_id=args.request_id,
                json_mode=args.json,
            )
        )
    if command == "select":
        return asyncio.run(
            _select_command(
                home,
                args.plugin_id,
                args.digest,
                expected_digest=args.expected_digest,
                json_mode=args.json,
            )
        )
    if command in {"enable", "disable", "restart", "check"}:
        return asyncio.run(_lifecycle_command(home, command, args.plugin_id, args.json))
    if command == "update":
        return asyncio.run(
            _update_command(
                home,
                args.plugin_id,
                args.source,
                ref=args.ref,
                subdirectory=args.subdirectory,
                request_id=args.request_id,
                json_mode=args.json,
            )
        )
    if command == "reload":
        return asyncio.run(
            _reload_command(
                home,
                args.plugin_id,
                args.source,
                ref=args.ref,
                subdirectory=args.subdirectory,
                request_id=args.request_id,
                json_mode=args.json,
            )
        )
    if command == "rollback":
        return asyncio.run(
            _rollback_command(home, args.plugin_id, args.digest, json_mode=args.json)
        )
    if command == "remove":
        return asyncio.run(
            _remove_command(
                home,
                args.plugin_id,
                digest=args.digest,
                purge_data=args.purge_data,
                request_id=args.request_id,
                json_mode=args.json,
            )
        )
    raise AssertionError(f"unknown plugin command: {command}")


async def _lifecycle_command(
    home: Path,
    command: str,
    plugin_id: str,
    json_mode: bool,
) -> int:
    async with _plugin_session(home, quiet=json_mode) as service:
        operation: Callable[[str], object]
        if command == "check":
            result = service.check(plugin_id)
        else:
            operation = getattr(service, command)
            result = await operation(plugin_id)  # type: ignore[misc]
    return _emit(result, json_mode=json_mode)


async def _plugin_contributed_command(home: Path, args) -> int:
    async with _plugin_session(home, quiet=args.json) as service:
        if args.plugin_command == "commands":
            values = service.list_commands()
            result = None
        else:
            values = None
            result = await service.execute_command(
                args.command_id,
                args.argument,
                session_id=args.session_id,
                workspace=args.workspace,
            )
    if values is not None:
        return _emit_many(values, json_mode=args.json)
    assert result is not None
    return _emit(result, json_mode=args.json)


async def _configure_command(
    home: Path,
    candidate: PluginConfigurationCandidate,
    *,
    needs_runtime: bool,
    request_id: str | None,
    json_mode: bool,
) -> int:
    if needs_runtime:
        async with _plugin_session(home, quiet=json_mode) as service:
            result = await service.configure(candidate, request_id=request_id)
    else:
        result = await build_local_plugin_service(home).configure(candidate, request_id=request_id)
    return _emit(result, json_mode=json_mode)


def _configuration_candidate(service: PluginService, args) -> PluginConfigurationCandidate:
    if args.file:
        if args.credentials is not None:
            raise ValueError("--file already contains credential references")
        candidate = PluginConfigurationCandidate.model_validate_json(
            Path(args.file).read_text(encoding="utf-8")
        )
        if candidate.plugin_id != args.plugin_id or (
            args.digest is not None and candidate.package_digest != args.digest
        ):
            raise ValueError("Configuration file target does not match the command target")
        if (
            args.expected_revision is not None
            and args.expected_revision != candidate.expected_configuration_revision_id
        ):
            raise ValueError("Configuration file baseline does not match --expected-revision")
        return candidate
    view = service.configuration(args.plugin_id, package_digest=args.digest)
    if args.values is None and (view.schema_document is not None or args.credentials is None):
        raise ValueError(
            "Provide --values or --file; credential-only plugins may use --credentials"
        )
    candidate = view.candidate(
        values=_json_object(args.values, option="--values") if args.values is not None else {},
        credential_refs=parse_credential_bindings(
            _json_object(args.credentials, option="--credentials")
        )
        if args.credentials is not None
        else {},
    )
    if args.expected_revision is not None:
        candidate.expected_configuration_revision_id = args.expected_revision
    return candidate


async def _select_command(
    home: Path,
    plugin_id: str,
    digest: str,
    *,
    expected_digest: str | None,
    json_mode: bool,
) -> int:
    async with _plugin_session(home, quiet=json_mode) as service:
        result = await service.select(
            plugin_id,
            digest,
            expected_digest=expected_digest,
        )
    return _emit(result, json_mode=json_mode)


async def _update_command(
    home: Path,
    plugin_id: str,
    source: str,
    *,
    ref: str | None,
    subdirectory: str | None,
    request_id: str | None,
    json_mode: bool,
) -> int:
    service = build_local_plugin_service(home)
    result = await service.update(
        plugin_id,
        source,
        ref=ref,
        subdirectory=subdirectory,
        request_id=request_id,
    )
    return _emit(result, json_mode=json_mode)


async def _rollback_command(
    home: Path,
    plugin_id: str,
    digest: str,
    *,
    json_mode: bool,
) -> int:
    async with _plugin_session(home, quiet=json_mode) as service:
        result = await service.rollback(plugin_id, digest)
    return _emit(result, json_mode=json_mode)


async def _reload_command(
    home: Path,
    plugin_id: str,
    source: str | Path,
    *,
    ref: str | None,
    subdirectory: str | None,
    request_id: str | None,
    json_mode: bool,
) -> int:
    async with _plugin_session(home, quiet=json_mode) as service:
        result = await service.reload(
            plugin_id,
            source,
            ref=ref,
            subdirectory=subdirectory,
            request_id=request_id,
        )
    return _emit(result, json_mode=json_mode)


async def _remove_command(
    home: Path,
    plugin_id: str,
    *,
    digest: str | None,
    purge_data: bool,
    request_id: str | None,
    json_mode: bool,
) -> int:
    async with _plugin_session(home, quiet=json_mode) as service:
        result = await service.remove(
            plugin_id,
            package_digest=digest,
            purge_data=purge_data,
            request_id=request_id,
        )
    return _emit(result, json_mode=json_mode)


@asynccontextmanager
async def _plugin_session(home: Path, *, quiet: bool) -> AsyncGenerator[PluginService]:
    """Keep plugin stdout out of the CLI's machine-readable JSON channel."""

    output = redirect_stdout(StringIO()) if quiet else nullcontext()
    with output:
        async with open_local_plugins(home) as service:
            yield service


def _json_object(value: str, *, option: str) -> dict[str, object]:
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{option} must be a JSON object: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{option} must be a JSON object")
    return {str(key): item for key, item in raw.items()}


def _emit(value, *, json_mode: bool) -> int:
    if json_mode:
        print(value.model_dump_json(exclude_none=True))
    elif isinstance(value, PluginConfigurationResult):
        print(f"configure: {value.plugin_id}: {value.outcome} ({value.status}, {value.scope})")
        for diagnostic in value.diagnostics:
            print(f"[{diagnostic.severity}] {diagnostic.code}: {diagnostic.message}")
    elif isinstance(value, PluginConfigurationPreview):
        print(f"configuration preview: {'valid' if value.valid else 'invalid'} ({value.scope})")
        for issue in value.issues:
            print(f"{issue.path}: {issue.code}: {issue.message}")
    elif isinstance(value, PluginActionResult):
        suffix = f" ({value.package_digest})" if value.package_digest else ""
        print(f"{value.action}: {value.plugin_id}: {value.status}{suffix}")
        for diagnostic in value.diagnostics:
            print(f"[{diagnostic.severity}] {diagnostic.code}: {diagnostic.message}")
        if value.retained_by:
            print("Package code retained by: " + ", ".join(value.retained_by))
    else:
        print(
            json.dumps(
                value.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


def _emit_many(values: Sequence[OuroModel], *, json_mode: bool) -> int:
    if json_mode:
        print(
            json.dumps(
                [value.model_dump(mode="json", exclude_none=True) for value in values],
                ensure_ascii=False,
            )
        )
    else:
        for value in values:
            raw = value.model_dump(mode="json", exclude_none=True)
            if "command_id" in raw:
                parts = [raw.get("command_id", "")]
                if raw.get("usage"):
                    parts[0] = f"{parts[0]} {raw['usage']}"
                parts.append(raw.get("description", ""))
                print("\t".join(parts))
                continue
            print(
                f"{raw.get('plugin_id', '')}\t{raw.get('version', '')}\t"
                f"{raw.get('status', '')}\t{raw.get('package_digest', '')}"
            )
    return 0
