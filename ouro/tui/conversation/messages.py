"""Messages emitted by conversation widgets."""

from __future__ import annotations

from textual.message import Message

_REASONING_ENTRY_META = "ouro_reasoning_entry"


_TRACE_ENTRY_META = "ouro_trace_entry"


_TOOL_NAVIGATION_META = "ouro_tool_navigation"


_MESSAGE_META = "ouro_message"


class ToolNavigationRequested(Message):
    """A SpawnAgent tool presentation requested its durable child Session."""

    def __init__(self, *, parent_run_id: str, parent_tool_call_id: str) -> None:
        super().__init__()
        self.parent_run_id = parent_run_id
        self.parent_tool_call_id = parent_tool_call_id


class MessageCopyRequested(Message):
    """A message frame requested a verbatim copy of its source text."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text
