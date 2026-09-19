"""Incremental display projection with a single, validated completion boundary."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ouro.core.provider import ProviderCompletion, ProviderStreamEvent, ToolCallDelta
from ouro.providers.responses_codec import continuation_from_output, output_item, responses_usage
from ouro.providers.responses_errors import ResponsesError
from ouro.providers.responses_items import ResponseItems


@dataclass
class _Text:
    parts: list[str] = field(default_factory=list)
    complete: bool = False

    def delta(self, value: object) -> str:
        if self.complete or not isinstance(value, str):
            raise ResponsesError("conflicting_delta")
        self.parts.append(value)
        return value

    def finish(self, value: object) -> str:
        if not isinstance(value, str):
            raise ResponsesError("invalid_event")
        prefix = "".join(self.parts)
        if not value.startswith(prefix) or (self.complete and value != prefix):
            raise ResponsesError("conflicting_delta")
        suffix = value[len(prefix) :]
        self.parts = [value]
        self.complete = True
        return suffix


@dataclass
class _Tool:
    ordinal: int
    call_id: str = ""
    name: str = ""
    arguments: _Text = field(default_factory=_Text)


class ResponsesStreamDecoder:
    """One response. Native output indexes never become canonical tool ordinals."""

    def __init__(self, *, provider_id: str, model_id: str, scope: tuple[str, ...]) -> None:
        self._provider_id = provider_id
        self._model_id = model_id
        self._scope = scope
        self._items = ResponseItems()
        self._text: dict[tuple[int, str, int], _Text] = {}
        self._tools: dict[int, _Tool] = {}
        self._call_ids: dict[str, int] = {}
        self._terminal: dict[str, Any] | None = None
        self._finished = False

    def feed(self, event: dict[str, Any]) -> list[ProviderStreamEvent]:
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ResponsesError("invalid_event")
        if self._terminal is not None or self._finished:
            raise ResponsesError(
                "duplicate_terminal" if kind == "response.completed" else "output_after_terminal"
            )
        if kind == "response.incomplete":
            # No successful done means Core cannot execute even syntactically complete
            # tool fragments from an unsuccessful response.
            raise ResponsesError("response_incomplete")
        if kind in {"response.failed", "error"}:
            raise ResponsesError("response_failed")
        if kind == "response.completed":
            response = event.get("response")
            if (
                not isinstance(response, dict)
                or response.get("status") != "completed"
                or response.get("error") is not None
                or response.get("incomplete_details") is not None
            ):
                raise ResponsesError("invalid_terminal")
            self._terminal = response
            return []
        if kind in {"response.output_item.added", "response.output_item.done"}:
            index = _index(event, "output_index")
            item = event.get("item")
            complete = kind == "response.output_item.done"
            self._items.observe(index, item, complete=complete)
            if not isinstance(item, dict):
                raise ResponsesError("invalid_output_item")
            if complete:
                return self._complete_item(index, output_item(item))
            if item["type"] == "function_call":
                return self._tool_header(index, item)
            if item["type"] not in {"message", "reasoning"}:
                raise ResponsesError("unsupported_output_item")
            return []
        lanes = {
            "response.output_text": ("message", "output_text", "content_index", "text"),
            "response.refusal": ("message", "refusal", "content_index", "refusal"),
            "response.reasoning_summary_text": (
                "reasoning",
                "summary_text",
                "summary_index",
                "text",
            ),
        }
        stem, _, suffix = kind.rpartition(".")
        if stem in lanes and suffix in {"delta", "done"}:
            item_kind, lane, coordinate, field_name = lanes[stem]
            index = self._event_item(event, item_kind)
            part = _index(event, coordinate)
            progress = self._text.setdefault((index, lane, part), _Text())
            text = (
                progress.delta(event.get("delta"))
                if suffix == "delta"
                else progress.finish(event.get(field_name))
            )
            return _display(lane, text)
        if stem == "response.function_call_arguments" and suffix in {"delta", "done"}:
            index = self._event_item(event, "function_call")
            tool = self._tool(index)
            text = (
                tool.arguments.delta(event.get("delta"))
                if suffix == "delta"
                else tool.arguments.finish(event.get("arguments"))
            )
            return _tool_delta(tool.ordinal, arguments=text)
        # Lifecycle, annotations, content-part notices and future metadata are not
        # independent replay items. Supported complete item shapes are checked above.
        return []

    def finish(self) -> list[ProviderStreamEvent]:
        if self._finished:
            raise ResponsesError("duplicate_terminal")
        if self._terminal is None:
            raise ResponsesError("missing_terminal")
        self._finished = True
        items = self._items.finish(self._terminal.get("output", []))
        events: list[ProviderStreamEvent] = []
        for index, item in enumerate(items):
            events.extend(self._complete_item(index, output_item(item)))
        if [self._tools[index].ordinal for index in sorted(self._tools)] != list(
            range(len(self._tools))
        ):
            raise ResponsesError("conflicting_output_item")
        usage = responses_usage(self._terminal.get("usage"))
        if items:
            events.append(
                ProviderStreamEvent(
                    type="continuation",
                    continuation=continuation_from_output(
                        items,
                        provider_id=self._provider_id,
                        model_id=self._model_id,
                        scope=self._scope,
                    ),
                )
            )
        events.append(
            ProviderStreamEvent(
                type="done",
                usage=usage,
                completion=ProviderCompletion(
                    finish_reason="tool_call" if self._tools else "stop",
                    provider_finish_reason="completed",
                ),
            )
        )
        return events

    def _event_item(self, event: dict[str, Any], kind: str) -> int:
        index = _index(event, "output_index")
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or not item_id:
            raise ResponsesError("invalid_event")
        self._items.observe(index, {"type": kind, "id": item_id}, complete=False)
        return index

    def _tool(self, index: int) -> _Tool:
        if index not in self._tools:
            self._tools[index] = _Tool(ordinal=len(self._tools))
        return self._tools[index]

    def _tool_header(self, index: int, item: dict[str, Any]) -> list[ProviderStreamEvent]:
        tool = self._tool(index)
        call_id, name = item.get("call_id"), item.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise ResponsesError("invalid_output_item")
        if (tool.call_id and tool.call_id != call_id) or (tool.name and tool.name != name):
            raise ResponsesError("conflicting_output_item")
        if self._call_ids.get(call_id, index) != index:
            raise ResponsesError("duplicate_tool_call")
        self._call_ids[call_id] = index
        events = _tool_delta(
            tool.ordinal, call_id="" if tool.call_id else call_id, name="" if tool.name else name
        )
        tool.call_id, tool.name = call_id, name
        return events

    def _complete_item(self, index: int, item: dict[str, Any]) -> list[ProviderStreamEvent]:
        if item["type"] == "function_call":
            events = self._tool_header(index, item)
            tool = self._tool(index)
            events.extend(
                _tool_delta(tool.ordinal, arguments=tool.arguments.finish(item["arguments"]))
            )
            return events
        parts = item.get("content" if item["type"] == "message" else "summary", [])
        events = []
        keys = set()
        for part_index, part in enumerate(parts):
            lane = part["type"]
            key = (index, lane, part_index)
            keys.add(key)
            progress = self._text.setdefault(key, _Text())
            events.extend(
                _display(lane, progress.finish(part["refusal" if lane == "refusal" else "text"]))
            )
        if any(key[0] == index and key not in keys for key in self._text):
            raise ResponsesError("conflicting_output_item")
        return events


def _index(event: dict[str, Any], key: str) -> int:
    value = event.get(key)
    if type(value) is not int or value < 0:
        raise ResponsesError("invalid_event")
    return value


def _display(lane: str, text: str) -> list[ProviderStreamEvent]:
    if not text:
        return []
    if lane == "summary_text":
        return [ProviderStreamEvent(type="reasoning_delta", reasoning_content=text)]
    return [ProviderStreamEvent(type="text_delta", text=text)]


def _tool_delta(
    ordinal: int, *, call_id: str = "", name: str = "", arguments: str = ""
) -> list[ProviderStreamEvent]:
    if not (call_id or name or arguments):
        return []
    return [
        ProviderStreamEvent(
            type="tool_call_delta",
            tool_call_delta=ToolCallDelta(
                index=ordinal, id_delta=call_id, name_delta=name, arguments_delta=arguments
            ),
        )
    ]


async def responses_events(
    chunks: AsyncIterator[dict[str, Any]],
    *,
    provider_id: str,
    model_id: str,
    scope: tuple[str, ...],
) -> AsyncGenerator[ProviderStreamEvent]:
    decoder = ResponsesStreamDecoder(provider_id=provider_id, model_id=model_id, scope=scope)
    try:
        async for chunk in chunks:
            for event in decoder.feed(chunk):
                yield event
        for event in decoder.finish():
            yield event
    finally:
        close = getattr(chunks, "aclose", None)
        if close is not None:
            await close()
