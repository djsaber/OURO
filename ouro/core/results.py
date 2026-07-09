from __future__ import annotations

from pydantic import Field

from ouro.core.base import OuroModel
from ouro.core.events import EventRecord
from ouro.core.state import RunRecord, SessionRecord
from ouro.core.tools import ToolResult


class RunResult(OuroModel):
    session: SessionRecord
    run: RunRecord
    final_text: str = ""
    events: list[EventRecord] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
