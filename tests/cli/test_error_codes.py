from __future__ import annotations

import pytest

from ouro.cli.errors import error_exit_code
from ouro.cli.run import _run_exit_code
from ouro.core.errors import PersistenceError
from ouro.core.results import RunResult
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolResult


def _result(status: str, *, tool_error: bool = False) -> RunResult:
    session = SessionRecord()
    run = RunRecord(session_id=session.session_id, status=status)  # type: ignore[arg-type]
    tool_results = (
        [ToolResult(tool_call_id="call_1", tool_name="Read", is_error=True)] if tool_error else []
    )
    return RunResult(session=session, run=run, tool_results=tool_results)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("completed", 0),
        ("waiting_approval", 4),
        ("waiting_user", 4),
        ("aborted", 5),
        ("failed", 1),
    ],
)
def test_run_exit_codes(status: str, expected: int) -> None:
    assert _run_exit_code(_result(status)) == expected


def test_failed_run_with_tool_error_uses_tool_exit_code() -> None:
    assert _run_exit_code(_result("failed", tool_error=True)) == 6


def test_persistence_error_uses_persistence_exit_code() -> None:
    assert error_exit_code(PersistenceError("disk full")) == 7
