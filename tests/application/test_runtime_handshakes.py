import asyncio
from types import SimpleNamespace

import pytest

from ouro.application.contracts.base import Diagnostic
from ouro.application.contracts.execution import RuntimeActivitySnapshot, RuntimeStatusSnapshot
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from tests.application.helpers import wait_for_activity, wait_for_runtime_event


@pytest.mark.parametrize("kind", ["runtime", "supervisor", "durable"])
async def test_runtime_handshake_reports_early_failure_without_waiting_for_event(kind):
    session = SessionRecord()
    failure = "Storage unavailable"
    run = RunRecord(session_id=session.session_id, status="failed", error=failure)
    if kind == "supervisor":
        result = RuntimeStatusSnapshot(
            session_id=session.session_id,
            status="failed",
            diagnostics=(
                Diagnostic(
                    code="startup_failed",
                    severity="error",
                    component="runtime",
                    message=failure,
                ),
            ),
        )
    elif kind == "runtime":
        result = RunResult(session=session, run=run)
    else:
        result = run

    async def finish():
        return result

    task = asyncio.create_task(finish())
    with pytest.raises(AssertionError, match=f"Run ended before model stream: failed;.*{failure}"):
        await wait_for_runtime_event(asyncio.Event(), task, description="model stream")
    assert task.result() is result


async def test_runtime_handshake_preserves_execution_ownership():
    started, release = asyncio.Event(), asyncio.Event()

    async def run():
        started.set()
        await release.wait()
        return RuntimeStatusSnapshot(session_id="ses_owned", status="completed")

    task = asyncio.create_task(run())
    try:
        await wait_for_runtime_event(started, task, description="model stream")
        assert not task.done()
        assert not release.is_set()
    finally:
        release.set()
        result = await task
    assert result.status == "completed"


async def test_runtime_handshake_propagates_execution_exception():
    async def fail():
        raise OSError("Cannot open session")

    task = asyncio.create_task(fail())
    with pytest.raises(OSError, match="Cannot open session"):
        await wait_for_runtime_event(asyncio.Event(), task, description="model stream")


@pytest.mark.parametrize("already_completed", [False, True])
async def test_activity_wait_observes_initial_snapshot_or_next_notification(already_completed):
    completed = RuntimeActivitySnapshot(
        session_id="ses_observed", status="completed", activity="completed"
    )
    reads = 0

    async def get():
        nonlocal reads
        reads += 1
        assert not already_completed, "An already-current snapshot needs no future notification"
        return SimpleNamespace(activity=completed, diagnostic=None)

    subscription = SimpleNamespace(
        initial_activity=completed if already_completed else None, get=get
    )
    assert await wait_for_activity(subscription, status="completed") is completed
    assert reads == (0 if already_completed else 1)
