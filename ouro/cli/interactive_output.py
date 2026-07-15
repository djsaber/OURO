from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ouro.cli.terminal_status import StatusTone, TerminalStatusLine
from ouro.core.events import EventRecord
from ouro.mcp.models import MCPServerConfig, MCPServerRecord

OutputFunc = Callable[[str], None]

_DIM_ITALIC = "\x1b[2;3m"
_DIM = "\x1b[2m"
_YELLOW = "\x1b[33m"
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_RESET = "\x1b[0m"
_MAX_TOOL_ARGUMENT_PREVIEW = 240


@dataclass
class _ToolCallPreview:
    tool_name: str = ""
    argument_chunks: list[str] = field(default_factory=list)

    def append(self, payload: dict[str, Any]) -> None:
        self.tool_name += _payload_text(payload, "name_delta")
        arguments_delta = _payload_text(payload, "arguments_delta")
        if arguments_delta:
            self.argument_chunks.append(arguments_delta)

    @property
    def arguments(self) -> str:
        return "".join(self.argument_chunks)


class InteractiveOutput:
    """Project lifecycle events into bounded interactive terminal output."""

    def __init__(
        self,
        output_func: OutputFunc,
        *,
        status_line: TerminalStatusLine | None = None,
    ) -> None:
        self.output_func = output_func
        self.status_line = status_line or TerminalStatusLine(output_func)
        self.streamed_run_ids: set[str] = set()
        self.in_reasoning = False
        self.streamed_text = False
        self.tool_call_previews: dict[tuple[str, str, int], _ToolCallPreview] = {}
        self.active_tool_calls: dict[tuple[str, str], str] = {}

    async def start_mcp_connection(self, configs: tuple[MCPServerConfig, ...]) -> None:
        if not configs:
            return
        names = ", ".join(config.name for config in configs)
        await self.status_line.start(
            "mcp_connection",
            f"Connecting MCP · {len(configs)} server(s) · {names}",
        )

    async def finish_mcp_connection(self, records: tuple[MCPServerRecord, ...]) -> None:
        if not records:
            return
        await self.status_line.clear()
        connected = [record for record in records if record.status == "connected"]
        failed = [record for record in records if record.status == "failed"]
        tool_count = sum(len(record.tool_names) for record in connected)
        summary = f"MCP · {len(connected)} connected"
        if failed:
            summary += f" · {len(failed)} failed"
        summary += f" · {tool_count} tool(s)"
        rendered_summary = f"{_DIM}{summary}{_RESET}" if self.status_line.animated else summary
        self._write(f"{rendered_summary}\n")
        for record in records:
            if record.status == "connected":
                symbol = self._status_symbol("✓", _GREEN)
                details = f"{record.config.transport} · {len(record.tool_names)} tool(s)"
            else:
                symbol = self._status_symbol("✗", _RED)
                details = f"failed · {record.error or 'unknown error'}"
            identity = _truncate(
                _single_line(f"{record.config.name} [{record.config.server_id}]"),
                80,
            )
            self._write(f"  {symbol} {identity} · {_truncate(_single_line(details), 140)}\n")

    async def close(self) -> None:
        await self.status_line.clear()
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
        elif self.streamed_text:
            self._write("\n")
        self.in_reasoning = False
        self.streamed_text = False
        self.streamed_run_ids.clear()
        self.tool_call_previews.clear()
        self.active_tool_calls.clear()

    async def handle_event(self, event: EventRecord) -> None:
        if event.run_id is None:
            return
        if event.event_type == "model_text_delta":
            text = event.payload.get("text")
            if isinstance(text, str) and text:
                self.streamed_run_ids.add(event.run_id)
                await self._stream_text_delta(text)
        elif event.event_type == "model_reasoning_delta":
            reasoning = event.payload.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                self.streamed_run_ids.add(event.run_id)
                await self._stream_reasoning_delta(reasoning)
        elif event.event_type == "model_tool_call_delta":
            self.streamed_run_ids.add(event.run_id)
            await self._stream_tool_call_delta(event)
        elif event.event_type == "model_message_completed":
            tool_call_count = event.payload.get("tool_call_count")
            if isinstance(tool_call_count, int) and tool_call_count > 0:
                await self._finish_tool_call_turn(event.run_id, event.turn_id)
        elif event.event_type == "tool_call_started":
            await self._start_tool_execution(event)
        elif event.event_type == "tool_call_completed":
            await self._finish_tool_execution(event)
        elif event.event_type in {"tool_batch_completed", "tool_batch_aborted"}:
            await self._clear_tool_execution(event.run_id)
        elif event.event_type == "compaction_started":
            await self.status_line.clear()
            trigger = str(event.payload.get("trigger") or "auto")
            self.output_func(f"Context {trigger} compaction in progress...")
        elif event.event_type == "compaction_completed":
            await self.status_line.clear()
            covered = int(event.payload.get("covered_message_count") or 0)
            self.output_func(f"Context compaction completed ({covered} messages covered).")
        elif event.event_type == "compaction_failed":
            await self.status_line.clear()
            error = event.payload.get("error") or "unknown error"
            self.output_func(f"Context compaction failed: {error}")

    async def finish_run(self, run_id: str) -> bool:
        await self.status_line.clear()
        self._remove_active_tools(run_id)
        streamed = run_id in self.streamed_run_ids
        self.streamed_run_ids.discard(run_id)
        if not streamed:
            return False
        if self.in_reasoning:
            self._write(_RESET)
            self.in_reasoning = False
        await self._finish_incomplete_tool_calls(run_id)
        self._write("\n")
        self.streamed_text = False
        return True

    async def _stream_text_delta(self, text: str) -> None:
        await self.status_line.clear()
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
            self.in_reasoning = False
        self.streamed_text = True
        self._write(text)

    async def _stream_reasoning_delta(self, reasoning: str) -> None:
        await self.status_line.clear()
        if self.streamed_text:
            self._write("\n")
            self.streamed_text = False
        if not self.in_reasoning:
            self._write(_DIM_ITALIC)
            self.in_reasoning = True
        self._write(reasoning)

    async def _stream_tool_call_delta(self, event: EventRecord) -> None:
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
            self.in_reasoning = False
        if self.streamed_text:
            self._write("\n")
            self.streamed_text = False
        index = event.payload.get("index")
        if not isinstance(index, int) or index < 0:
            return
        turn_id = event.turn_id or ""
        key = (event.run_id or "", turn_id, index)
        preview = self.tool_call_previews.setdefault(key, _ToolCallPreview())
        preview.append(event.payload)
        message = self._tool_input_status(event.run_id or "", turn_id)
        if self.status_line.semantic == "tool_input":
            self.status_line.update(message)
        else:
            await self.status_line.start("tool_input", message)

    async def _finish_tool_call_turn(self, run_id: str, turn_id: str | None) -> None:
        keys = sorted(
            (
                key
                for key in self.tool_call_previews
                if key[0] == run_id and key[1] == (turn_id or "")
            ),
            key=lambda key: key[2],
        )
        if not keys:
            return
        await self.status_line.clear()
        self._end_streamed_content_line()
        for key in keys:
            preview = self.tool_call_previews.pop(key)
            self._write(f"{self._render_tool_call(preview, final=True)}\n")

    async def _finish_incomplete_tool_calls(self, run_id: str) -> None:
        keys = sorted(
            (key for key in self.tool_call_previews if key[0] == run_id),
            key=lambda key: (key[1], key[2]),
        )
        if not keys:
            return
        await self.status_line.clear()
        self._end_streamed_content_line()
        for key in keys:
            preview = self.tool_call_previews.pop(key)
            self._write(f"{self._render_tool_call(preview)} {_DIM}(incomplete){_RESET}\n")

    async def _start_tool_execution(self, event: EventRecord) -> None:
        tool_call_id = _payload_text(event.payload, "tool_call_id")
        if not tool_call_id:
            return
        tool_name = _payload_text(event.payload, "tool_name") or "unknown tool"
        self.active_tool_calls[(event.run_id or "", tool_call_id)] = tool_name
        message = self._tool_execution_status(event.run_id or "")
        if self.status_line.semantic == "tool_execution":
            self.status_line.update(message)
        else:
            await self.status_line.start("tool_execution", message)

    async def _finish_tool_execution(self, event: EventRecord) -> None:
        tool_call_id = _payload_text(event.payload, "tool_call_id")
        if not tool_call_id:
            return
        key = (event.run_id or "", tool_call_id)
        tool_name = self.active_tool_calls.pop(
            key,
            _payload_text(event.payload, "tool_name") or "unknown tool",
        )
        status = _payload_text(event.payload, "status")
        is_error = event.payload.get("is_error") is True
        if status == "aborted":
            tone: StatusTone = "warning"
            result_text = "aborted"
        elif is_error or status == "failed":
            tone = "error"
            result_text = "failed"
        else:
            tone = "success"
            result_text = "completed"
        await self.status_line.finish(f"{tool_name} · {result_text}", tone=tone)
        remaining = self._tool_execution_status(event.run_id or "")
        if remaining:
            await self.status_line.start("tool_execution", remaining)

    async def _clear_tool_execution(self, run_id: str) -> None:
        self._remove_active_tools(run_id)
        if self.status_line.semantic == "tool_execution":
            await self.status_line.clear()

    def _remove_active_tools(self, run_id: str) -> None:
        for key in [key for key in self.active_tool_calls if key[0] == run_id]:
            self.active_tool_calls.pop(key, None)

    def _tool_input_status(self, run_id: str, turn_id: str) -> str:
        previews = [
            preview
            for key, preview in sorted(self.tool_call_previews.items())
            if key[0] == run_id and key[1] == turn_id
        ]
        names = [preview.tool_name for preview in previews if preview.tool_name]
        if not names:
            return "Receiving tool input"
        if len(names) == 1:
            return f"Receiving tool input · {names[0]}"
        return f"Receiving {len(previews)} tool inputs · {', '.join(names)}"

    def _tool_execution_status(self, run_id: str) -> str:
        names = [
            name
            for (active_run_id, _), name in self.active_tool_calls.items()
            if active_run_id == run_id
        ]
        if not names:
            return ""
        if len(names) == 1:
            return f"Running tool · {names[0]}"
        return f"Running {len(names)} tools · {', '.join(names)}"

    def _end_streamed_content_line(self) -> None:
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
            self.in_reasoning = False
        if self.streamed_text:
            self._write("\n")
            self.streamed_text = False

    @staticmethod
    def _render_tool_call(preview: _ToolCallPreview, *, final: bool = False) -> str:
        tool_name = _single_line(preview.tool_name or "preparing tool")
        arguments = (
            _format_completed_arguments(preview.arguments)
            if final
            else _single_line(preview.arguments)
        )
        arguments = _truncate(arguments, _MAX_TOOL_ARGUMENT_PREVIEW)
        return f"{_DIM}>{_RESET} {_YELLOW}{tool_name}{_RESET}({_DIM}{arguments}{_RESET})"

    def _status_symbol(self, symbol: str, color: str) -> str:
        return f"{color}{symbol}{_RESET}" if self.status_line.animated else symbol

    def _write(self, text: str) -> None:
        if self.output_func is print:
            sys.stdout.write(text)
            sys.stdout.flush()
            return
        self.output_func(text)


def _payload_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _format_completed_arguments(arguments: str) -> str:
    if not arguments:
        return ""
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return _single_line(arguments)
    return json.dumps(parsed, ensure_ascii=False)


def _single_line(value: str) -> str:
    return value.replace("\r", "\\r").replace("\n", "\\n")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
