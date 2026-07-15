from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ouro.cli.terminal_status import (
    SpinnerSemantic,
    StatusTone,
    TerminalStatusLine,
    TerminalStatusRegion,
)
from ouro.core.events import EventRecord
from ouro.mcp.models import MCPServerConfig, MCPServerRecord

OutputFunc = Callable[[str], None]

_DIM_ITALIC = "\x1b[2;3m"
_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_RESET = "\x1b[0m"
_TOOL_NAME_STYLE = "\x1b[1;36m"
_MAX_TOOL_NAME_LENGTH = 32
_MAX_TOOL_ARGUMENT_PREVIEW = 120


@dataclass
class _ToolCallPreview:
    tool_name: str = ""
    argument_chunks: list[str] = field(default_factory=list)
    phase: str = "generating parameters"
    tone: StatusTone | None = None

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
        tool_lines: TerminalStatusRegion | None = None,
    ) -> None:
        self.output_func = output_func
        self.status_line = status_line or TerminalStatusLine(output_func)
        self.tool_lines = tool_lines or TerminalStatusRegion(output_func)
        self.streamed_run_ids: set[str] = set()
        self.in_reasoning = False
        self.streamed_text = False
        self.tool_call_previews: dict[tuple[str, str, int], _ToolCallPreview] = {}
        self.tool_call_keys: dict[tuple[str, str], tuple[str, str, int]] = {}
        self.active_tool_calls: dict[tuple[str, str], tuple[str, str, int]] = {}

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
        await self.tool_lines.clear()
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
        elif self.streamed_text:
            self._write("\n")
        self.in_reasoning = False
        self.streamed_text = False
        self.streamed_run_ids.clear()
        self.tool_call_previews.clear()
        self.tool_call_keys.clear()
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
        elif event.event_type == "tool_call_requested":
            await self._bind_tool_call(event)
        elif event.event_type == "approval_requested":
            await self._set_tool_phase(event, "approval required", semantic="tool_execution")
        elif event.event_type == "approval_resolved":
            await self._resolve_tool_interaction(event)
        elif event.event_type == "ask_user_requested":
            await self._set_tool_phase(event, "waiting for input", semantic="tool_execution")
        elif event.event_type == "ask_user_resolved":
            await self._resolve_tool_interaction(event)
        elif event.event_type == "tool_batch_started":
            await self._start_tool_batch(event)
        elif event.event_type == "tool_call_started":
            await self._start_tool_execution(event)
        elif event.event_type == "tool_call_completed":
            await self._finish_tool_execution(event)
        elif event.event_type in {"tool_batch_completed", "tool_batch_aborted"}:
            await self._finish_tool_batch(event)
        elif event.event_type == "compaction_started":
            self._end_streamed_content_line()
            await self.status_line.start(
                "compaction",
                _compaction_activity(event.payload),
            )
        elif event.event_type == "compaction_progress":
            message = _compaction_activity(event.payload)
            if self.status_line.semantic == "compaction":
                self.status_line.update(message)
            else:
                await self.status_line.start("compaction", message)
        elif event.event_type == "compaction_completed":
            await self.status_line.finish(
                _compaction_completed(event.payload),
                tone="success",
            )
        elif event.event_type == "compaction_failed":
            error = event.payload.get("error") or "unknown error"
            await self.status_line.finish(
                f"Context compaction failed · {error}",
                tone="error",
            )
        elif event.event_type == "compaction_skipped":
            reason = str(event.payload.get("reason") or "not required").replace("_", " ")
            await self.status_line.finish(
                f"Context compaction skipped · {reason}",
                tone="neutral",
            )

    async def finish_run(self, run_id: str) -> bool:
        await self.status_line.clear()
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

    async def suspend_tools_for_prompt(self) -> None:
        """Keep tool state while freeing the cursor for an interactive prompt."""

        await self.tool_lines.suspend()

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
        await self.tool_lines.upsert(
            _tool_row_key(key),
            "tool_input",
            self._tool_message(preview, include_arguments=False),
        )

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
        self._end_streamed_content_line()
        for key in keys:
            preview = self.tool_call_previews[key]
            preview.phase = "ready"
            await self.tool_lines.upsert(
                _tool_row_key(key),
                "tool_input",
                self._tool_message(preview),
            )

    async def _finish_incomplete_tool_calls(self, run_id: str) -> None:
        keys = sorted(
            (key for key in self.tool_call_previews if key[0] == run_id),
            key=lambda key: (key[1], key[2]),
        )
        if not keys:
            return
        self._end_streamed_content_line()
        for key in keys:
            preview = self.tool_call_previews[key]
            if preview.tone is not None:
                continue
            preview.phase = "incomplete"
            preview.tone = "warning"
            await self.tool_lines.finish(
                _tool_row_key(key),
                self._tool_message(preview),
                tone="warning",
            )
        await self.tool_lines.commit()
        self._remove_tool_state(run_id)

    async def _bind_tool_call(self, event: EventRecord) -> None:
        tool_call_id = _payload_text(event.payload, "tool_call_id")
        source_index = event.payload.get("source_index")
        if not tool_call_id or not isinstance(source_index, int):
            return
        key = (event.run_id or "", event.turn_id or "", source_index)
        preview = self.tool_call_previews.setdefault(key, _ToolCallPreview())
        if not preview.tool_name:
            preview.tool_name = _payload_text(event.payload, "tool_name")
        if not preview.argument_chunks:
            arguments = event.payload.get("arguments")
            if isinstance(arguments, dict):
                preview.argument_chunks.append(json.dumps(arguments, ensure_ascii=False))
        self.tool_call_keys[(event.run_id or "", tool_call_id)] = key
        await self.tool_lines.upsert(
            _tool_row_key(key),
            "tool_input",
            self._tool_message(preview),
        )

    async def _set_tool_phase(
        self,
        event: EventRecord,
        phase: str,
        *,
        semantic: SpinnerSemantic,
    ) -> None:
        key = self._tool_key_for_event(event)
        if key is None:
            return
        preview = self.tool_call_previews[key]
        preview.phase = phase
        await self.tool_lines.upsert(
            _tool_row_key(key),
            semantic,
            self._tool_message(preview),
        )

    async def _resolve_tool_interaction(self, event: EventRecord) -> None:
        key = self._tool_key_for_event(event)
        if key is None:
            return
        preview = self.tool_call_previews[key]
        status = _payload_text(event.payload, "tool_call_status")
        if status in {"rejected", "aborted"}:
            await self._finish_tool_row(key, status, "warning")
        elif status == "failed":
            await self._finish_tool_row(key, "failed", "error")
        elif status == "completed":
            await self._finish_tool_row(key, "completed", "success")
        else:
            preview.phase = "approved" if event.event_type == "approval_resolved" else "ready"
            await self.tool_lines.upsert(
                _tool_row_key(key),
                "tool_execution",
                self._tool_message(preview),
            )

    async def _start_tool_batch(self, event: EventRecord) -> None:
        calls = event.payload.get("tool_calls")
        if not isinstance(calls, list):
            return
        for call in calls:
            if not isinstance(call, dict):
                continue
            synthetic = event.model_copy(update={"payload": call})
            key = self._tool_key_for_event(synthetic)
            if key is None:
                continue
            status = _payload_text(call, "status")
            if status in {"completed", "failed", "aborted", "rejected"}:
                tone: StatusTone = (
                    "success"
                    if status == "completed"
                    else "error"
                    if status == "failed"
                    else "warning"
                )
                await self._finish_tool_row(key, status, tone)
                continue
            preview = self.tool_call_previews[key]
            preview.phase = "queued"
            await self.tool_lines.upsert(
                _tool_row_key(key),
                "tool_execution",
                self._tool_message(preview),
            )

    async def _start_tool_execution(self, event: EventRecord) -> None:
        tool_call_id = _payload_text(event.payload, "tool_call_id")
        if not tool_call_id:
            return
        key = self._tool_key_for_event(event)
        if key is None:
            return
        preview = self.tool_call_previews[key]
        preview.phase = "running"
        self.active_tool_calls[(event.run_id or "", tool_call_id)] = key
        await self.tool_lines.upsert(
            _tool_row_key(key),
            "tool_execution",
            self._tool_message(preview),
        )

    async def _finish_tool_execution(self, event: EventRecord) -> None:
        tool_call_id = _payload_text(event.payload, "tool_call_id")
        if not tool_call_id:
            return
        call_key = (event.run_id or "", tool_call_id)
        key = self.active_tool_calls.pop(call_key, None) or self._tool_key_for_event(event)
        if key is None:
            return
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
        await self._finish_tool_row(key, result_text, tone)

    async def _finish_tool_batch(self, event: EventRecord) -> None:
        run_id = event.run_id or ""
        turn_id = event.turn_id or ""
        matching = [
            key
            for key in self.tool_call_previews
            if key[0] == run_id and (not turn_id or key[1] == turn_id)
        ]
        for key in matching:
            preview = self.tool_call_previews[key]
            if preview.tone is not None:
                continue
            phase = "aborted" if event.event_type == "tool_batch_aborted" else "finished"
            tone: StatusTone = "warning" if phase == "aborted" else "neutral"
            await self._finish_tool_row(key, phase, tone)
        await self.tool_lines.commit()
        self._remove_tool_state(run_id, turn_id=turn_id or None)

    async def _finish_tool_row(
        self,
        key: tuple[str, str, int],
        phase: str,
        tone: StatusTone,
    ) -> None:
        preview = self.tool_call_previews[key]
        preview.phase = phase
        preview.tone = tone
        await self.tool_lines.finish(
            _tool_row_key(key),
            self._tool_message(preview),
            tone=tone,
        )

    def _tool_key_for_event(self, event: EventRecord) -> tuple[str, str, int] | None:
        run_id = event.run_id or ""
        tool_call_id = _payload_text(event.payload, "tool_call_id")
        if tool_call_id:
            mapped = self.tool_call_keys.get((run_id, tool_call_id))
            if mapped is not None:
                return mapped
        source_index = event.payload.get("source_index")
        if not isinstance(source_index, int):
            return None
        key = (run_id, event.turn_id or "", source_index)
        preview = self.tool_call_previews.setdefault(key, _ToolCallPreview())
        if not preview.tool_name:
            preview.tool_name = _payload_text(event.payload, "tool_name")
        if tool_call_id:
            self.tool_call_keys[(run_id, tool_call_id)] = key
        return key

    def _remove_tool_state(self, run_id: str, *, turn_id: str | None = None) -> None:
        keys = [
            key
            for key in self.tool_call_previews
            if key[0] == run_id and (turn_id is None or key[1] == turn_id)
        ]
        for key in keys:
            self.tool_call_previews.pop(key, None)
        for call_key, preview_key in list(self.tool_call_keys.items()):
            if preview_key in keys:
                self.tool_call_keys.pop(call_key, None)
        for call_key, preview_key in list(self.active_tool_calls.items()):
            if preview_key in keys:
                self.active_tool_calls.pop(call_key, None)

    def _end_streamed_content_line(self) -> None:
        if self.in_reasoning:
            self._write(f"{_RESET}\n")
            self.in_reasoning = False
        if self.streamed_text:
            self._write("\n")
            self.streamed_text = False

    def _tool_message(
        self,
        preview: _ToolCallPreview,
        *,
        include_arguments: bool = True,
    ) -> str:
        tool_name = _truncate(
            _single_line(preview.tool_name or "preparing tool"),
            _MAX_TOOL_NAME_LENGTH,
        )
        if self.tool_lines.animated:
            tool_name = f"{_TOOL_NAME_STYLE}{tool_name}{_RESET}"
        arguments = _format_completed_arguments(preview.arguments) if include_arguments else ""
        arguments = _truncate(arguments, _MAX_TOOL_ARGUMENT_PREVIEW)
        status = f"{tool_name} · {preview.phase}"
        return f"{status} · ({arguments})" if include_arguments else status

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


def _tool_row_key(key: tuple[str, str, int]) -> str:
    return f"{key[0]}:{key[1]}:{key[2]}"


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


def _compaction_activity(payload: dict[str, Any]) -> str:
    trigger = str(payload.get("trigger") or "auto")
    message = f"Compacting context · {trigger}"
    phase = str(payload.get("phase") or "")
    if not phase:
        return message
    phase_label = "generating summary" if phase == "generating" else phase.replace("_", " ")
    return f"{message} · {phase_label}"


def _compaction_completed(payload: dict[str, Any]) -> str:
    parts = ["Context compaction completed"]
    covered = payload.get("covered_message_count")
    if isinstance(covered, int):
        noun = "message" if covered == 1 else "messages"
        parts.append(f"{covered} {noun}")
    before = payload.get("tokens_before")
    after = payload.get("tokens_after")
    if isinstance(before, int) and isinstance(after, int):
        parts.append(f"{before} → {after} tokens")
    return " · ".join(parts)
