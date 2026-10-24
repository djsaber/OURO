import asyncio

import pytest

from ouro.application.contracts.base import Diagnostic
from ouro.application.contracts.execution import RuntimeStatusSnapshot
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from tests.application.helpers import wait_for_runtime_event


@pytest.mark.parametrize("supervised", [False, True])
async def test_runtime_handshake_reports_early_failure_without_waiting_for_event(supervised):
    session = SessionRecord()
    failure = "Storage unavailable"
    result = (
        RuntimeStatusSnapshot(
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
        if supervised
        else RunResult(
            session=session,
            run=RunRecord(session_id=session.session_id, status="failed", error=failure),
        )
    )

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
