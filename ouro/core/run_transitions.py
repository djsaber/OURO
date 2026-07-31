from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ouro.core.base import utc_now
from ouro.core.state import (
    ACTIVE_RUN_STATUSES,
    RunRecord,
    SessionRecord,
    SessionStatus,
)

_INITIAL_RUN_STATUSES = {"queued", "running"}


@dataclass(frozen=True, slots=True)
class RunSessionTransitionPlan:
    """Pure result of one authoritative Run transition and Session projection."""

    run: RunRecord
    session: SessionRecord


def session_state_for_run(run: RunRecord) -> tuple[SessionStatus, str | None]:
    """Project one authoritative Run onto its owning Session's coarse state."""

    if run.status == "waiting_approval":
        return "waiting_approval", run.run_id
    if run.status == "waiting_user":
        return "waiting_user", run.run_id
    if run.status in ACTIVE_RUN_STATUSES:
        return "running", run.run_id
    if run.status == "failed" and run.kind == "agent":
        return "failed", None
    return "idle", None


def plan_run_transition(
    session: SessionRecord,
    *,
    current_run: RunRecord | None,
    next_run: RunRecord,
    now: datetime | None = None,
) -> RunSessionTransitionPlan:
    """Validate a Run state edge and derive the corresponding Session projection.

    The function is deliberately unaware of persistence, locking, events, and
    message bookkeeping. Callers supply the fully prepared next Run and the
    latest Session basis, then persist the returned Run before its projection.
    """

    if session.session_id != next_run.session_id:
        raise ValueError("Run transition Session identity does not match its Run")
    if current_run is None:
        if next_run.status not in _INITIAL_RUN_STATUSES:
            raise ValueError(f"new Run cannot start in {next_run.status} state")
    else:
        if current_run.session_id != session.session_id:
            raise ValueError("current Run does not belong to the transition Session")
        if current_run.run_id != next_run.run_id:
            raise ValueError("Run transition cannot change Run identity")
        if current_run.status not in ACTIVE_RUN_STATUSES and next_run.status != current_run.status:
            raise ValueError(
                f"terminal Run cannot transition from {current_run.status} to {next_run.status}"
            )

    if next_run.status in ACTIVE_RUN_STATUSES and next_run.ended_at is not None:
        raise ValueError(f"active Run {next_run.run_id} cannot have ended_at")
    if next_run.status not in ACTIVE_RUN_STATUSES and next_run.ended_at is None:
        raise ValueError(f"terminal Run {next_run.run_id} requires ended_at")

    projected_status, active_run_id = session_state_for_run(next_run)
    transition_time = now or utc_now()
    projected_session = session.model_copy(
        update={
            "status": projected_status,
            "active_run_id": active_run_id,
            "last_run_id": next_run.run_id,
            "updated_at": transition_time,
            "last_activity_at": transition_time,
        }
    )
    return RunSessionTransitionPlan(run=next_run, session=projected_session)
