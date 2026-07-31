from __future__ import annotations

from datetime import timedelta

import pytest

from ouro.core.base import utc_now
from ouro.core.run_transitions import plan_run_transition, session_state_for_run
from ouro.core.state import RunKind, RunRecord, RunStatus, SessionRecord


@pytest.mark.parametrize(
    ("status", "expected_session_status"),
    [
        ("queued", "running"),
        ("running", "running"),
        ("waiting_child", "running"),
        ("waiting_user", "waiting_user"),
        ("waiting_approval", "waiting_approval"),
    ],
)
def test_active_run_statuses_project_one_active_session_owner(
    status: RunStatus,
    expected_session_status: str,
) -> None:
    run = RunRecord(session_id="ses_active", status=status)

    assert session_state_for_run(run) == (expected_session_status, run.run_id)


@pytest.mark.parametrize(
    ("status", "kind", "expected_session_status"),
    [
        ("completed", "agent", "idle"),
        ("aborted", "agent", "idle"),
        ("paused", "agent", "idle"),
        ("recoverable", "agent", "idle"),
        ("failed", "agent", "failed"),
        ("failed", "compaction", "idle"),
    ],
)
def test_terminal_run_statuses_clear_session_ownership(
    status: RunStatus,
    kind: RunKind,
    expected_session_status: str,
) -> None:
    run = RunRecord(session_id="ses_terminal", status=status, kind=kind)

    assert session_state_for_run(run) == (expected_session_status, None)


@pytest.mark.parametrize(
    ("name", "current_status", "next_status", "expected_session_status"),
    [
        ("start", "queued", "running", "running"),
        ("wait", "running", "waiting_user", "waiting_user"),
        ("resume", "waiting_approval", "running", "running"),
        ("pause", "running", "paused", "idle"),
        ("complete", "running", "completed", "idle"),
        ("fail", "running", "failed", "failed"),
        ("abort", "waiting_child", "aborted", "idle"),
        ("recover", "running", "recoverable", "idle"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_transition_contracts_project_run_and_session_together(
    name: str,
    current_status: RunStatus,
    next_status: RunStatus,
    expected_session_status: str,
) -> None:
    del name
    session = SessionRecord(session_id="ses_transition")
    current = RunRecord(
        run_id="run_transition",
        session_id=session.session_id,
        status=current_status,
    )
    transition_time = utc_now()
    terminal = next_status not in {
        "queued",
        "running",
        "waiting_user",
        "waiting_approval",
        "waiting_child",
    }
    next_run = current.model_copy(
        update={
            "status": next_status,
            "ended_at": transition_time if terminal else None,
        }
    )

    plan = plan_run_transition(
        session,
        current_run=current,
        next_run=next_run,
        now=transition_time,
    )

    assert plan.run is next_run
    assert plan.session.status == expected_session_status
    assert plan.session.active_run_id == (None if terminal else next_run.run_id)
    assert plan.session.last_run_id == next_run.run_id
    assert plan.session.updated_at == transition_time
    assert plan.session.last_activity_at == transition_time


@pytest.mark.parametrize("status", ["queued", "running"])
def test_new_run_registration_accepts_only_initial_states(status: RunStatus) -> None:
    session = SessionRecord(session_id="ses_new")
    run = RunRecord(session_id=session.session_id, status=status)

    plan = plan_run_transition(session, current_run=None, next_run=run)

    assert plan.session.status == "running"
    assert plan.session.active_run_id == run.run_id


def test_terminal_run_cannot_be_reopened() -> None:
    ended_at = utc_now()
    current = RunRecord(
        run_id="run_terminal",
        session_id="ses_terminal",
        status="completed",
        ended_at=ended_at,
    )
    reopened = current.model_copy(update={"status": "running", "ended_at": None})

    with pytest.raises(ValueError, match="terminal Run cannot transition"):
        plan_run_transition(
            SessionRecord(session_id=current.session_id),
            current_run=current,
            next_run=reopened,
        )


def test_transition_rejects_identity_changes() -> None:
    current = RunRecord(run_id="run_one", session_id="ses_one", status="running")
    next_run = current.model_copy(update={"run_id": "run_two"})

    with pytest.raises(ValueError, match="cannot change Run identity"):
        plan_run_transition(
            SessionRecord(session_id=current.session_id),
            current_run=current,
            next_run=next_run,
        )


def test_transition_rejects_inconsistent_end_timestamp() -> None:
    current = RunRecord(run_id="run_time", session_id="ses_time", status="running")

    with pytest.raises(ValueError, match="requires ended_at"):
        plan_run_transition(
            SessionRecord(session_id=current.session_id),
            current_run=current,
            next_run=current.model_copy(update={"status": "completed"}),
        )

    with pytest.raises(ValueError, match="cannot have ended_at"):
        plan_run_transition(
            SessionRecord(session_id=current.session_id),
            current_run=current,
            next_run=current.model_copy(update={"ended_at": utc_now() + timedelta(seconds=1)}),
        )
