from __future__ import annotations

from ouro.core.base import OuroModel
from ouro.core.state import RunRecord, SessionRecord


class RunResult(OuroModel):
    """One invocation summary; historical facts are read through explicit queries."""

    session: SessionRecord
    run: RunRecord
    final_text: str = ""
    usage: dict[str, int | float] | None = None
    error_code: str | None = None
    had_tool_error: bool = False
