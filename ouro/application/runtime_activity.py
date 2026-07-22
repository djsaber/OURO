from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from ouro.application.session_models import SessionContextStatus
from ouro.application.supervisor_models import (
    RuntimeActivity,
    RuntimeActivitySnapshot,
    RuntimeToolCallActivity,
    SupervisorRunStatus,
)
from ouro.core.context import ContextMetrics
from ouro.core.events import EventRecord
from ouro.core.state import ACTIVE_RUN_STATUSES, RunRecord, SessionRecord
from ouro.core.tool_batches import ToolCallStatus
from ouro.sessions.base import SessionSnapshot

_MODEL_ACTIVITIES = {
    "preparing_model",
    "waiting_model",
    "streaming_reasoning",
    "streaming_text",
    "streaming_tool_call",
    "finalizing_response",
    "compacting",
}
_TERMINAL_ACTIVITY: dict[str, RuntimeActivity] = {
    "completed": "completed",
    "aborted": "aborted",
    "failed": "failed",
    "paused": "paused",
    "recoverable": "recoverable",
}


def queued_runtime_activity(
    session_id: str,
    *,
    sequence: int = 0,
) -> RuntimeActivitySnapshot:
    return RuntimeActivitySnapshot(
        session_id=session_id,
        status="queued",
        activity="queued",
        sequence=sequence,
    )


def project_runtime_activity(
    snapshot: SessionSnapshot,
    *,
    run_id: str | None = None,
    status_override: SupervisorRunStatus | None = None,
    context_status: SessionContextStatus | None = None,
) -> RuntimeActivitySnapshot:
    run = _selected_run(snapshot, run_id=run_id)
    if run is None:
        status = status_override or "idle"
        return RuntimeActivitySnapshot(
            session_id=snapshot.session.session_id,
            status=status,
            activity=runtime_activity_for_status(status),
            context_status=context_status,
            sequence=_latest_sequence(snapshot.events),
        )

    activity = RuntimeActivitySnapshot(
        session_id=snapshot.session.session_id,
        run_id=run.run_id,
        status="queued",
        activity="queued",
        context_status=context_status,
        sequence=0,
        updated_at=run.started_at,
    )
    for event in sorted(snapshot.events, key=lambda item: (item.sequence, item.event_id)):
        if event.run_id == run.run_id:
            activity = reduce_runtime_activity(activity, event)
    activity = _overlay_run(activity, run)
    if status_override is not None and status_override != activity.status:
        activity = activity.model_copy(
            update={
                "status": status_override,
                "activity": runtime_activity_for_status(status_override),
            }
        )
    return activity


def project_runtime_activity_state(
    session: SessionRecord,
    run: RunRecord | None,
    *,
    sequence: int,
    status_override: SupervisorRunStatus | None = None,
    context_status: SessionContextStatus | None = None,
) -> RuntimeActivitySnapshot:
    """Project bounded current activity without replaying historical events."""

    if run is None:
        status = status_override or "idle"
        return RuntimeActivitySnapshot(
            session_id=session.session_id,
            status=status,
            activity=runtime_activity_for_status(status),
            context_status=context_status,
            sequence=sequence,
        )
    activity = RuntimeActivitySnapshot(
        session_id=session.session_id,
        run_id=run.run_id,
        status=run.status,
        activity=runtime_activity_for_status(run.status),
        context_status=context_status,
        sequence=sequence,
        updated_at=run.ended_at or run.started_at,
    )
    activity = _overlay_run(activity, run)
    if status_override is not None and status_override != activity.status:
        activity = activity.model_copy(
            update={
                "status": status_override,
                "activity": runtime_activity_for_status(status_override),
            }
        )
    return activity


def reduce_runtime_activity(
    current: RuntimeActivitySnapshot,
    event: EventRecord,
) -> RuntimeActivitySnapshot:
    if event.sequence <= current.sequence:
        return current

    payload = event.payload
    updates: dict[str, Any] = {
        "sequence": event.sequence,
        "updated_at": event.created_at,
    }
    if event.run_id is not None:
        updates["run_id"] = event.run_id
    turn_id = payload.get("turn_id")
    if isinstance(turn_id, str):
        updates["turn_id"] = turn_id

    event_type = event.event_type
    if event_type == "run_started":
        updates.update(_run_reset("running", "preparing_model"))
        updates["run_phase"] = "model_streaming"
    elif event_type == "model_request_started":
        updates.update(
            activity="waiting_model",
            status="running",
            run_phase="model_streaming",
            compaction_id=None,
        )
        context_status = _context_status_from_request(event)
        if context_status is not None:
            updates["context_status"] = context_status
    elif event_type == "run_retry_scheduled":
        updates.update(activity="waiting_model", status="running")
    elif event_type == "model_reasoning_delta":
        updates.update(activity="streaming_reasoning", status="running")
    elif event_type == "model_text_delta":
        updates.update(activity="streaming_text", status="running")
    elif event_type == "model_tool_call_delta":
        updates.update(activity="streaming_tool_call", status="running")
    elif event_type == "model_message_completed":
        tool_call_count = payload.get("tool_call_count")
        updates["activity"] = (
            "preparing_tools"
            if isinstance(tool_call_count, int) and tool_call_count > 0
            else "finalizing_response"
        )
        context_status = current.context_status
        usage = payload.get("usage")
        context_id = _string_or_none(payload.get("context_id"))
        if context_status is not None and context_status.context_id == context_id:
            input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
            updates["context_status"] = context_status.model_copy(
                update={
                    "completed_at": event.created_at,
                    "provider_reported_input_tokens": (
                        input_tokens
                        if isinstance(input_tokens, int) and input_tokens >= 0
                        else None
                    ),
                }
            )
    elif event_type == "tool_batch_planned":
        updates.update(
            activity="preparing_tools",
            status="running",
            run_phase="tool_preflight",
            batch_phase="planned",
            batch_id=_string_or_none(payload.get("batch_id")),
            tool_calls=(),
        )
    elif event_type == "tool_call_requested":
        updates["tool_calls"] = _upsert_tool_call(current.tool_calls, payload, "planned")
    elif event_type == "approval_requested":
        updates["tool_calls"] = _update_tool_call_status(
            current.tool_calls,
            _string_or_none(payload.get("tool_call_id")),
            "awaiting_approval",
        )
    elif event_type == "ask_user_requested":
        updates["tool_calls"] = _update_tool_call_status(
            current.tool_calls,
            _string_or_none(payload.get("tool_call_id")),
            "resolving_input" if payload.get("will_suspend") is False else "waiting_user",
        )
    elif event_type in {"approval_resolved", "ask_user_resolved"}:
        call_status = _tool_call_status(payload.get("tool_call_status"))
        if call_status is not None:
            updates["tool_calls"] = _update_tool_call_status(
                current.tool_calls,
                _string_or_none(payload.get("tool_call_id")),
                call_status,
            )
    elif event_type == "run_status_changed":
        run_status = _supervisor_status(payload.get("to_status", payload.get("status")))
        if run_status is not None:
            updates["status"] = run_status
            if run_status in {"waiting_approval", "waiting_user", "waiting_child"}:
                updates["activity"] = run_status
                updates["run_phase"] = (
                    "subagent_join" if run_status == "waiting_child" else "human_interaction"
                )
                updates["batch_phase"] = (
                    "awaiting_child"
                    if run_status == "waiting_child"
                    else "awaiting_interaction"
                )
            elif run_status in _TERMINAL_ACTIVITY:
                updates["activity"] = _TERMINAL_ACTIVITY[run_status]
    elif event_type == "tool_batch_started":
        updates.update(
            activity="executing_tools",
            status="running",
            run_phase="tool_execution",
            batch_phase="executing",
        )
        started_calls = _tool_calls_from_payload(payload.get("tool_calls"))
        updates["tool_calls"] = (
            started_calls
            if started_calls is not None
            else tuple(
                item.model_copy(update={"status": "ready"})
                if item.status in {"planned", "awaiting_approval", "waiting_user"}
                else item
                for item in current.tool_calls
            )
        )
    elif event_type == "tool_call_started":
        updates.update(activity="executing_tools", status="running")
        updates["tool_calls"] = _upsert_tool_call(current.tool_calls, payload, "running")
    elif event_type == "tool_call_completed":
        call_status = _tool_call_status(payload.get("status")) or (
            "failed" if payload.get("is_error") is True else "completed"
        )
        updates["tool_calls"] = _upsert_tool_call(current.tool_calls, payload, call_status)
    elif event_type == "tool_batch_results_committing":
        updates.update(
            activity="committing_results",
            status="running",
            run_phase="result_commit",
            batch_phase="committing_results",
        )
    elif event_type == "tool_batch_completed":
        updates.update(
            activity="preparing_model",
            status="running",
            run_phase="model_streaming",
            batch_id=None,
            batch_phase=None,
            tool_calls=(),
        )
    elif event_type == "tool_batch_aborted":
        updates.update(activity="aborted", status="aborted", batch_phase="aborted")
    elif event_type in {"compaction_started", "compaction_progress"}:
        updates.update(
            activity="compacting",
            status="running",
            compaction_id=_string_or_none(payload.get("compaction_id")),
        )
    elif event_type in {"compaction_completed", "compaction_skipped"}:
        updates.update(activity="preparing_model", compaction_id=None)
    elif event_type == "compaction_failed":
        updates["compaction_id"] = None
    elif event_type in {
        "run_completed",
        "run_aborted",
        "run_failed",
        "run_paused",
    }:
        terminal_status = {
            "run_completed": "completed",
            "run_aborted": "aborted",
            "run_failed": "failed",
            "run_paused": "paused",
        }[event_type]
        updates.update(status=terminal_status, activity=terminal_status, compaction_id=None)

    return current.model_copy(update=updates)


def runtime_activity_changed(
    previous: RuntimeActivitySnapshot,
    current: RuntimeActivitySnapshot,
) -> bool:
    excluded = {"sequence", "updated_at"}
    return previous.model_dump(exclude=excluded) != current.model_dump(exclude=excluded)


def _selected_run(snapshot: SessionSnapshot, *, run_id: str | None) -> RunRecord | None:
    if run_id is not None:
        return next((run for run in snapshot.runs if run.run_id == run_id), None)
    active = [run for run in snapshot.runs if run.status in ACTIVE_RUN_STATUSES]
    if active:
        return active[-1]
    if snapshot.session.last_run_id is not None:
        for run in reversed(snapshot.runs):
            if run.run_id == snapshot.session.last_run_id:
                return run
    return snapshot.runs[-1] if snapshot.runs else None


def _overlay_run(
    current: RuntimeActivitySnapshot,
    run: RunRecord,
) -> RuntimeActivitySnapshot:
    batch = run.active_tool_batch
    updates: dict[str, Any] = {
        "run_id": run.run_id,
        "status": run.status,
        "run_phase": run.phase,
        "batch_id": batch.batch_id if batch is not None else None,
        "batch_phase": batch.phase if batch is not None else None,
        "tool_calls": (
            tuple(
                RuntimeToolCallActivity(
                    tool_call_id=call.request.id,
                    tool_name=call.request.name,
                    status=call.status,
                    source_index=call.request.source_index,
                )
                for call in sorted(batch.calls, key=lambda item: item.request.source_index)
            )
            if batch is not None
            else ()
        ),
    }
    if run.status == "queued":
        updates["activity"] = "queued"
    elif run.status in {"waiting_approval", "waiting_user", "waiting_child"}:
        updates["activity"] = run.status
    elif run.status in _TERMINAL_ACTIVITY:
        updates["activity"] = _TERMINAL_ACTIVITY[run.status]
    elif run.phase == "tool_preflight":
        updates["activity"] = "preparing_tools"
    elif run.phase == "human_interaction":
        updates["activity"] = (
            "waiting_approval" if run.pending_approval_id is not None else "waiting_user"
        )
    elif run.phase == "tool_execution":
        updates["activity"] = (
            "executing_tools"
            if batch is not None and batch.phase == "executing"
            else "preparing_tools"
        )
    elif run.phase == "result_commit":
        updates["activity"] = "committing_results"
    elif current.activity not in _MODEL_ACTIVITIES:
        updates["activity"] = "preparing_model"
    return current.model_copy(update=updates)


def _run_reset(status: SupervisorRunStatus, activity: RuntimeActivity) -> dict[str, Any]:
    return {
        "status": status,
        "activity": activity,
        "turn_id": None,
        "batch_id": None,
        "batch_phase": None,
        "tool_calls": (),
        "compaction_id": None,
    }


def _upsert_tool_call(
    calls: tuple[RuntimeToolCallActivity, ...],
    payload: dict[str, Any],
    status: ToolCallStatus,
) -> tuple[RuntimeToolCallActivity, ...]:
    tool_call_id = _string_or_none(payload.get("tool_call_id"))
    if tool_call_id is None:
        return calls
    tool_name = str(payload.get("tool_name") or "unknown")
    source_index = payload.get("source_index")
    normalized_index = source_index if isinstance(source_index, int) and source_index >= 0 else 0
    updated: list[RuntimeToolCallActivity] = []
    matched = False
    for item in calls:
        if item.tool_call_id == tool_call_id:
            updated.append(
                item.model_copy(
                    update={
                        "tool_name": tool_name if tool_name != "unknown" else item.tool_name,
                        "status": status,
                    }
                )
            )
            matched = True
        else:
            updated.append(item)
    if not matched:
        updated.append(
            RuntimeToolCallActivity(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=status,
                source_index=normalized_index,
            )
        )
    return tuple(sorted(updated, key=lambda item: (item.source_index, item.tool_call_id)))


def _update_tool_call_status(
    calls: tuple[RuntimeToolCallActivity, ...],
    tool_call_id: str | None,
    status: ToolCallStatus,
) -> tuple[RuntimeToolCallActivity, ...]:
    if tool_call_id is None:
        return calls
    return tuple(
        item.model_copy(update={"status": status}) if item.tool_call_id == tool_call_id else item
        for item in calls
    )


def _tool_calls_from_payload(value: object) -> tuple[RuntimeToolCallActivity, ...] | None:
    if not isinstance(value, list):
        return None
    calls: list[RuntimeToolCallActivity] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        tool_call_id = _string_or_none(item.get("tool_call_id"))
        tool_name = _string_or_none(item.get("tool_name"))
        status = _tool_call_status(item.get("status"))
        source_index = item.get("source_index")
        if (
            tool_call_id is None
            or tool_name is None
            or status is None
            or not isinstance(source_index, int)
            or source_index < 0
        ):
            return None
        calls.append(
            RuntimeToolCallActivity(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=status,
                source_index=source_index,
            )
        )
    return tuple(sorted(calls, key=lambda item: (item.source_index, item.tool_call_id)))


def runtime_activity_for_status(status: SupervisorRunStatus) -> RuntimeActivity:
    if status == "running":
        return "preparing_model"
    if status in {"idle", "queued", "waiting_approval", "waiting_user", "waiting_child"}:
        return status
    return _TERMINAL_ACTIVITY[status]


def _supervisor_status(value: object) -> SupervisorRunStatus | None:
    if value in {
        "idle",
        "queued",
        "running",
        "waiting_user",
        "waiting_approval",
        "waiting_child",
        "completed",
        "aborted",
        "failed",
        "paused",
        "recoverable",
    }:
        return cast(SupervisorRunStatus, value)
    return None


def _tool_call_status(value: object) -> ToolCallStatus | None:
    if value in {
        "planned",
        "awaiting_approval",
        "waiting_user",
        "resolving_input",
        "ready",
        "rejected",
        "running",
        "completed",
        "failed",
        "aborted",
        "unknown_outcome",
    }:
        return cast(ToolCallStatus, value)
    return None


def _latest_sequence(events: Iterable[EventRecord]) -> int:
    return max((event.sequence for event in events), default=0)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _context_status_from_request(event: EventRecord) -> SessionContextStatus | None:
    payload = event.payload
    context_id = _string_or_none(payload.get("context_id"))
    provider_id = _string_or_none(payload.get("provider"))
    model_id = _string_or_none(payload.get("model"))
    turn_id = _string_or_none(payload.get("turn_id"))
    metrics_payload = payload.get("context_metrics")
    model_round = payload.get("round")
    if (
        context_id is None
        or provider_id is None
        or model_id is None
        or turn_id is None
        or event.run_id is None
        or not isinstance(model_round, int)
        or model_round < 1
        or not isinstance(metrics_payload, dict)
    ):
        return None
    try:
        metrics = ContextMetrics.model_validate(metrics_payload)
    except ValueError:
        return None
    return SessionContextStatus(
        context_id=context_id,
        session_id=event.session_id,
        run_id=event.run_id,
        turn_id=turn_id,
        model_round=model_round,
        provider_id=provider_id,
        endpoint_id=_string_or_none(payload.get("endpoint")),
        model_id=model_id,
        prepared_at=event.created_at,
        metrics=metrics,
        active_compaction_id=_string_or_none(payload.get("active_compaction_id")),
    )
