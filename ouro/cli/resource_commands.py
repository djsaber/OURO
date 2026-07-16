from __future__ import annotations

import json
import sys
from pathlib import Path

from ouro.application.resources import ResourceService
from ouro.cli.errors import error_exit_code
from ouro.core.errors import OuroError
from ouro.deployment import SessionBackend
from ouro.local_application import build_local_session_services


def import_resource(
    session_id: str,
    source_path: str,
    *,
    home: str | Path | None = None,
    media_type: str | None = None,
    json_mode: bool = False,
    session_backend: SessionBackend | None = None,
) -> int:
    try:
        result = _resources(home, session_backend).import_resource(
            session_id,
            source_path,
            media_type=media_type,
        )
    except (OuroError, OSError, ValueError) as exc:
        return _error(exc, json_mode=json_mode)
    payload = {
        "status": "ok",
        "created": result.created,
        "resource": result.resource.model_dump(mode="json"),
        "diagnostics": [item.model_dump(mode="json") for item in result.diagnostics],
    }
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        action = "Imported" if result.created else "Attached existing"
        print(f"{action} resource: {result.resource.resource_id}")
    return 0


def list_resources(
    session_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
    session_backend: SessionBackend | None = None,
) -> int:
    try:
        result = _resources(home, session_backend).list_resources(session_id)
    except (OuroError, OSError, ValueError) as exc:
        return _error(exc, json_mode=json_mode)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "session_id": session_id,
                    "resources": [item.model_dump(mode="json") for item in result.resources],
                },
                ensure_ascii=False,
            )
        )
    elif not result.resources:
        print("No resources.")
    else:
        for resource in result.resources:
            print(
                f"{resource.resource_id}\t{resource.media_type}\t"
                f"{resource.size}\t{resource.source.name}"
            )
    return 0


def show_resource(
    session_id: str,
    resource_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
    session_backend: SessionBackend | None = None,
) -> int:
    try:
        resource = _resources(home, session_backend).show_resource(session_id, resource_id)
    except (OuroError, OSError, ValueError) as exc:
        return _error(exc, json_mode=json_mode)
    if json_mode:
        print(
            json.dumps(
                {"status": "ok", "resource": resource.model_dump(mode="json")},
                ensure_ascii=False,
            )
        )
    else:
        print(
            "\n".join(
                (
                    f"Resource: {resource.resource_id}",
                    f"Digest: {resource.digest}",
                    f"Media type: {resource.media_type}",
                    f"Size: {resource.size}",
                    f"Source: {resource.source.name}",
                    f"Created: {resource.created_at}",
                )
            )
        )
    return 0


def export_resource(
    session_id: str,
    resource_id: str,
    output: str,
    *,
    home: str | Path | None = None,
    force: bool = False,
    json_mode: bool = False,
    session_backend: SessionBackend | None = None,
) -> int:
    try:
        result = _resources(home, session_backend).export_resource(
            session_id,
            resource_id,
            output,
            overwrite=force,
        )
    except (OuroError, OSError, ValueError) as exc:
        return _error(exc, json_mode=json_mode)
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "resource_id": resource_id,
                    "path": str(result.destination),
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"Exported resource to: {result.destination}")
    return 0


def remove_resource(
    session_id: str,
    resource_id: str,
    *,
    home: str | Path | None = None,
    force: bool = False,
    json_mode: bool = False,
    session_backend: SessionBackend | None = None,
) -> int:
    if not force:
        confirm = input(f"Remove resource {resource_id} from session {session_id}? [y/N] ")
        if confirm.strip().lower() not in {"y", "yes"}:
            print(json.dumps({"status": "cancelled"}) if json_mode else "Cancelled.")
            return 0
    try:
        result = _resources(home, session_backend).remove_resource(session_id, resource_id)
    except (OuroError, OSError, ValueError) as exc:
        return _error(exc, json_mode=json_mode)
    if json_mode:
        print(json.dumps({"status": "ok", **result.model_dump(mode="json")}, ensure_ascii=False))
    else:
        print(f"Removed resource reference: {resource_id}")
    return 0


def _resources(
    home: str | Path | None,
    session_backend: SessionBackend | None,
) -> ResourceService:
    return build_local_session_services(home, session_backend=session_backend).resources


def _error(exc: Exception, *, json_mode: bool) -> int:
    if json_mode:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"Error: {exc}", file=sys.stderr)
    return error_exit_code(exc) if isinstance(exc, OuroError) else 1
