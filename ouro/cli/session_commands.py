from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from ouro.cli.errors import error_exit_code
from ouro.cli.run import build_default_runtime
from ouro.core.compaction import CompactionCommandResult
from ouro.core.errors import OuroError
from ouro.sessions import LocalSessionStore


def list_sessions(*, home: str | Path | None = None, json_mode: bool = False) -> int:
    try:
        store = _store(home)
        sessions = store.list_sessions()
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


def show_session(
    session_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        snapshot = _store(home).load_session(session_id)
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
        store = _store(home)
        session_dir = store.sessions_dir / session_id
        if not session_dir.exists():
            raise OuroError(f"session not found: {session_id}")
        if not force:
            confirm = input(f"Remove session {session_id}? [y/N] ").strip().lower()
            if confirm not in {"y", "yes"}:
                if json_mode:
                    print(json.dumps({"status": "cancelled"}, ensure_ascii=False))
                else:
                    print("Cancelled.")
                return 0
        shutil.rmtree(session_dir)
        store.rebuild_index()
    except OuroError as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc)
    except Exception as exc:  # noqa: BLE001
        _print_error(exc, json_mode=json_mode)
        return 1

    if json_mode:
        print(json.dumps({"status": "ok", "removed": session_id}, ensure_ascii=False))
    else:
        print(f"Session removed: {session_id}")
    return 0


def export_session(
    session_id: str,
    *,
    home: str | Path | None = None,
    output_path: str | None = None,
    json_mode: bool = False,
) -> int:
    try:
        snapshot = _store(home).load_session(session_id)
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
    json_mode: bool = False,
) -> int:
    try:
        snapshot = _store(home).load_session(session_id)
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
                    "messages": [
                        message_record.model_dump(mode="json")
                        for message_record in snapshot.messages
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 0

    if not snapshot.messages:
        print("No messages.")
        return 0

    for message in snapshot.messages:
        print(f"[{message.role}] {message.text_content}")
    return 0


def list_session_events(
    session_id: str,
    *,
    home: str | Path | None = None,
    run_id: str | None = None,
    json_mode: bool = False,
) -> int:
    try:
        snapshot = _store(home).load_session(session_id)
    except FileNotFoundError:
        error_message = f"session not found: {session_id}"
        _print_error(OuroError(error_message), json_mode=json_mode)
        return 1
    except OuroError as exc:
        _print_error(exc, json_mode=json_mode)
        return error_exit_code(exc)

    events = snapshot.events
    if run_id is not None:
        events = [event for event in events if event.run_id == run_id]

    if json_mode:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "events": [event.model_dump(mode="json") for event in events],
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
    return 0


async def compact_session(
    session_id: str,
    *,
    home: str | Path | None = None,
    json_mode: bool = False,
) -> int:
    try:
        runtime = build_default_runtime(home)
        result = await runtime.compact(session_id=session_id)
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
                    "model_route": compaction.metadata.get("model_route")
                    if compaction
                    else None,
                    "model_fallback_reason": compaction.metadata.get(
                        "model_fallback_reason"
                    )
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
        part
        for part in (compaction.model_provider, compaction.model_endpoint_id)
        if part
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
