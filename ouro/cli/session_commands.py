from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ouro.application.facade import ApplicationFacade
from ouro.application.resources import ResourceService
from ouro.application.session_models import (
    SessionCatalogQuery,
    SessionCatalogStatus,
    SessionSort,
)
from ouro.cli.errors import error_exit_code
from ouro.cli.run import build_default_runtime
from ouro.core.approvals import SessionApprovalMode
from ouro.core.compaction import CompactionCommandResult
from ouro.core.errors import OuroError
from ouro.resources import LocalResourceStore
from ouro.sessions import LocalSessionStore


def list_sessions(
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
    search: str | None = None,
    statuses: list[SessionCatalogStatus] | None = None,
    archived: bool | None = None,
    pinned: bool | None = None,
    workspace: str | None = None,
    model: str | None = None,
    tags: list[str] | None = None,
    sort: SessionSort = "updated_desc",
) -> int:
    try:
        query = SessionCatalogQuery(
            text=search,
            statuses=frozenset(statuses or []),
            archived=archived,
            pinned=pinned,
            workspace=workspace,
            model=model,
            tags=frozenset(tags or []),
            sort=sort,
        )
        sessions = _facade(home).session_catalog(query).entries
    except OuroError as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "sessions": [session.model_dump(mode="json") for session in sessions],
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not sessions:
        print("No sessions.")
        return 0

    lines = ["Sessions:"]
    for session in sessions:
        marker = "*" if session.status != "idle" else "-"
        title = session.title or "New session"
        model = session.current_model or "-"
        lines.append(f"{marker} {session.session_id} {session.status} {model} {title}")
    print("\n".join(lines))
    return 0


def rename_session(
    session_id: str,
    title: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        result = _facade(home).rename_session(session_id, title)
    except (OuroError, FileNotFoundError, ValueError) as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc) if isinstance(exc, OuroError) else 1
    if json_mode:
        print(
            json.dumps(
                {"status": "ok", "session": result.session.model_dump(mode="json")},
                ensure_ascii=False,
            )
        )
    else:
        print(f"Session renamed: {result.session.session_id} -> {result.session.title}")
    return 0


def set_session_approval_mode(
    session_id: str,
    approval_mode: SessionApprovalMode,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        result = _facade(home).set_session_approval_mode(session_id, approval_mode)
    except (OuroError, FileNotFoundError, ValueError) as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc) if isinstance(exc, OuroError) else 1
    if json_mode:
        print(
            json.dumps(
                {"status": "ok", "session": result.session.model_dump(mode="json")},
                ensure_ascii=False,
            )
        )
    else:
        print(f"Session approval mode: {session_id} -> {result.session.approval_mode}")
    return 0


def archive_session(
    session_id: str,
    *,
    archived: bool,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        result = _facade(home).archive_session(session_id, archived=archived)
    except (OuroError, FileNotFoundError) as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc) if isinstance(exc, OuroError) else 1
    if json_mode:
        print(
            json.dumps(
                {"status": "ok", "session": result.session.model_dump(mode="json")},
                ensure_ascii=False,
            )
        )
    else:
        verb = "archived" if archived else "unarchived"
        print(f"Session {verb}: {session_id}")
    return 0


def pin_session(
    session_id: str,
    *,
    pinned: bool,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        result = _facade(home).pin_session(session_id, pinned=pinned)
    except (OuroError, FileNotFoundError) as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc) if isinstance(exc, OuroError) else 1
    if json_mode:
        print(
            json.dumps(
                {"status": "ok", "session": result.session.model_dump(mode="json")},
                ensure_ascii=False,
            )
        )
    else:
        verb = "pinned" if pinned else "unpinned"
        print(f"Session {verb}: {session_id}")
    return 0


def branch_session(
    session_id: str,
    message_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        result = _facade(home).branch_session(session_id, message_id)
    except (OuroError, FileNotFoundError) as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc) if isinstance(exc, OuroError) else 1
    if json_mode:
        print(json.dumps({"status": "ok", **result.model_dump(mode="json")}, ensure_ascii=False))
    else:
        print(f"Session branched: {session_id} -> {result.session.session_id}")
    return 0


def replay_session(
    session_id: str,
    *,
    home: str | Path | None = None,
    run_id: str | None = None,
    json_mode: bool = False,
) -> int:
    try:
        result = _facade(home).replay_session(session_id, run_id=run_id)
    except (OuroError, FileNotFoundError) as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc) if isinstance(exc, OuroError) else 1
    if json_mode:
        print(json.dumps({"status": "ok", **result.model_dump(mode="json")}, ensure_ascii=False))
        return 0
    if not result.frames:
        print("No replay events.")
        return 0
    for frame in result.frames:
        event = frame.event
        print(f"[{frame.sequence}] {event.event_type} run={event.run_id}")
    return 0


def show_session(
    session_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        snapshot = _facade(home).open_session(session_id)
    except FileNotFoundError:
        error_message = f"session not found: {session_id}"
        _print_error(OuroError(error_message), json_mode=json_mode)
        return 1
    except OuroError as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "session": snapshot.session.model_dump(mode="json"),
                    "runs": [run.model_dump(mode="json") for run in snapshot.runs],
                },
                ensure_ascii=False,
            )
        )
        return 0

    session = snapshot.session
    print(
        "\n".join(
            [
                f"Session: {session.session_id}",
                f"Title: {session.title}",
                f"Status: {session.status}",
                f"Model: {session.current_model or '-'}",
                f"Approval mode: {session.approval_mode}",
                f"Workspace: {session.workspace or '-'}",
                f"Messages: {session.message_count}",
                f"Updated: {session.updated_at}",
            ]
        )
    )
    return 0


def remove_session(
    session_id: str,
    *,
    home: str | Path | None = None,
    force: bool = False,
    json_mode: bool = False,
) -> int:
    try:
        if not force:
            confirm = input(f"Move session {session_id} to trash? [y/N] ").strip().lower()
            if confirm not in {"y", "yes"}:
                if json_mode:
                    print(json.dumps({"status": "cancelled"}, ensure_ascii=False))
                else:
                    print("Cancelled.")
                return 0
        result = _facade(home).delete_session(session_id)
    except OuroError as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc)
    except Exception as exc:  # noqa: BLE001
        _print_error(exc, json_mode=json_mode)
        return 1

    if json_mode:
        print(
            json.dumps(
                {"status": "ok", "removed": session_id, "trash_id": result.trash_id},
                ensure_ascii=False,
            )
        )
    else:
        print(f"Session removed to trash: {session_id}")
    return 0


def export_session(
    session_id: str,
    *,
    home: str | Path | None = None,
    output_path: str | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = _facade(home)
        snapshot = facade.open_session(session_id)
        resources = facade.list_resources(session_id)
    except FileNotFoundError:
        error_message = f"session not found: {session_id}"
        _print_error(OuroError(error_message), json_mode=json_mode)
        return 1
    except OuroError as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    payload: dict[str, Any] = {
        "status": "ok",
        "session": snapshot.session.model_dump(mode="json"),
        "messages": [message.model_dump(mode="json") for message in snapshot.messages],
        "events": [event.model_dump(mode="json") for event in snapshot.events],
        "runs": [run.model_dump(mode="json") for run in snapshot.runs],
        "compactions": [compaction.model_dump(mode="json") for compaction in snapshot.compactions],
        "resources": [resource.model_dump(mode="json") for resource in resources.resources],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_path is not None:
        try:
            Path(output_path).write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            _print_error(exc, json_mode=json_mode)
            return 1
        if json_mode:
            print(json.dumps({"status": "ok", "path": output_path}, ensure_ascii=False))
        else:
            print(f"Exported session to: {output_path}")
        return 0

    if json_mode:
        print(text)
    else:
        print(text)
    return 0


def list_session_messages(
    session_id: str,
    *,
    home: str | Path | None = None,
    cursor: int = 0,
    limit: int = 100,
    json_mode: bool = False,
) -> int:
    try:
        page = _facade(home).session_messages(
            session_id,
            cursor=cursor,
            limit=limit,
        )
    except FileNotFoundError:
        error_message = f"session not found: {session_id}"
        _print_error(OuroError(error_message), json_mode=json_mode)
        return 1
    except (OuroError, ValueError) as exc:
        ouro_exc = exc if isinstance(exc, OuroError) else OuroError(str(exc))
        _print_error(ouro_exc, json_mode=json_mode)
        return error_exit_code(ouro_exc)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "messages": [
                        message_record.model_dump(mode="json") for message_record in page.messages
                    ],
                    "next_cursor": page.next_cursor,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not page.messages:
        print("No messages.")
        return 0

    for message in page.messages:
        print(f"[{message.role}] {message.text_content}")
    if page.next_cursor is not None:
        print(f"More messages available; continue with --cursor {page.next_cursor}")
    return 0


def list_session_events(
    session_id: str,
    *,
    home: str | Path | None = None,
    run_id: str | None = None,
    cursor: int = 0,
    limit: int = 100,
    json_mode: bool = False,
) -> int:
    try:
        page = _facade(home).session_events(
            session_id,
            cursor=cursor,
            limit=limit,
            run_id=run_id,
        )
    except FileNotFoundError:
        error_message = f"session not found: {session_id}"
        _print_error(OuroError(error_message), json_mode=json_mode)
        return 1
    except (OuroError, ValueError) as exc:
        ouro_exc = exc if isinstance(exc, OuroError) else OuroError(str(exc))
        _print_error(ouro_exc, json_mode=json_mode)
        return error_exit_code(ouro_exc)

    events = list(page.events)

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "events": [event.model_dump(mode="json") for event in events],
                    "next_cursor": page.next_cursor,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not events:
        print("No events.")
        return 0

    for event in events:
        print(f"[{event.event_type}] run={event.run_id} seq={event.sequence}")
    if page.next_cursor is not None:
        print(f"More events available; continue with --cursor {page.next_cursor}")
    return 0


async def compact_session(
    session_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        facade = ApplicationFacade.from_runtime(build_default_runtime(home))
        result = await facade.compact_session(session_id)
    except FileNotFoundError:
        error_message = f"session not found: {session_id}"
        _print_error(OuroError(error_message), json_mode=json_mode)
        return 1
    except (OuroError, ValueError, RuntimeError) as exc:
        ouro_exc = exc if isinstance(exc, OuroError) else OuroError(str(exc))
        _print_error(ouro_exc, json_mode=json_mode)
        return error_exit_code(ouro_exc)

    return _print_compaction_result(result, json_mode=json_mode)


def _print_compaction_result(result: CompactionCommandResult, *, json_mode: bool) -> int:
    compaction = result.compaction
    compaction_id = compaction.compaction_id if compaction is not None else None
    covered_count = len(compaction.covered_message_ids) if compaction is not None else 0

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "skipped" if result.skipped else "ok",
                    "session_id": result.session.session_id,
                    "run_id": result.run.run_id,
                    "skipped": result.skipped,
                    "compaction_id": compaction_id,
                    "covered_message_count": covered_count,
                    "model_provider": compaction.model_provider if compaction else None,
                    "model_endpoint_id": compaction.model_endpoint_id if compaction else None,
                    "model_id": compaction.model_id if compaction else None,
                    "model_route": compaction.metadata.get("model_route") if compaction else None,
                    "model_fallback_reason": compaction.metadata.get("model_fallback_reason")
                    if compaction
                    else None,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if result.skipped or compaction is None:
        print("Compaction skipped: no complete older turn is eligible.")
        return 0

    endpoint = "/".join(
        part for part in (compaction.model_provider, compaction.model_endpoint_id) if part
    )
    route = compaction.metadata.get("model_route")
    model_suffix = f" via {endpoint}" if endpoint else ""
    route_suffix = f" [{route}]" if route else ""
    print(
        f"Compaction completed: {compaction_id} ({covered_count} messages covered)"
        f"{model_suffix}{route_suffix}."
    )
    return 0


def _store(home: str | Path | None) -> LocalSessionStore:
    store = LocalSessionStore(home)
    store.initialize()
    return store


def _facade(home: str | Path | None) -> ApplicationFacade:
    sessions = _store(home)
    resource_store = LocalResourceStore(sessions.root)
    resource_store.initialize()
    resources = ResourceService(store=resource_store, sessions=sessions)
    return ApplicationFacade.with_resources(sessions, resources)


def _print_error(exc: Exception, *, json_mode: bool) -> None:
    message = str(exc)
    if json_mode:
        print(
            json.dumps(
                {"status": "failed", "error": message, "error_type": type(exc).__name__},
                ensure_ascii=False,
            )
        )
    else:
        print(f"Error: {message}", file=sys.stderr)
