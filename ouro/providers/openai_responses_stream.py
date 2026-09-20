"""Incremental display projection with a single, validated completion boundary."""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ouro.core.provider import ProviderCompletion, ProviderStreamEvent, ToolCallDelta
from ouro.providers.openai_responses_codec import (
    continuation_from_output,
    output_item,
    responses_usage,
)
from ouro.providers.openai_responses_errors import OpenAIResponsesError
from ouro.providers.openai_responses_items import OpenAIResponsesItemAccumulator


@dataclass
class _Text:
    parts: list[str] = field(default_factory=list)
    complete: bool = False

    def delta(self, value: object) -> str:
        if self.complete or not isinstance(value, str):
            raise OpenAIResponsesError("conflicting_delta")
        self.parts.append(value)
        return value

    def finish(self, value: object) -> str:
        if not isinstance(value, str):
            raise OpenAIResponsesError("invalid_event")
        prefix = "".join(self.parts)
        if not value.startswith(prefix) or (self.complete and value != prefix):
            raise OpenAIResponsesError("conflicting_delta")
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


class OpenAIResponsesStreamDecoder:
    """One response. Native output indexes never become canonical tool ordinals."""

    def __init__(self, *, provider_id: str, model_id: str, scope: tuple[str, ...]) -> None:
        self._provider_id = provider_id
        self._model_id = model_id
        self._scope = scope
        self._items = OpenAIResponsesItemAccumulator()
        self._text: dict[tuple[int, str, int], _Text] = {}
        self._tools: dict[int, _Tool] = {}
        self._call_ids: dict[str, int] = {}
        self._terminal: dict[str, Any] | None = None
        self._finished = False

    def feed(self, event: dict[str, Any]) -> list[ProviderStreamEvent]:
        kind = event.get("type")
        if not isinstance(kind, str):
            raise OpenAIResponsesError("invalid_event")
        if self._finished:
            self._require_open(kind)
        if kind == "response.incomplete":
            self._require_open(kind)
            # No successful done means Core cannot execute even syntactically complete
            # tool fragments from an unsuccessful response.
            raise OpenAIResponsesError("response_incomplete")
        if kind in {"response.failed", "error"}:
            self._require_open(kind)
            raise OpenAIResponsesError("response_failed")
        if kind == "response.completed":
            self._require_open(kind)
            response = event.get("response")
            if (
                not isinstance(response, dict)
                or response.get("status") != "completed"
                or response.get("error") is not None
                or response.get("incomplete_details") is not None
            ):
                raise OpenAIResponsesError("invalid_terminal")
            self._terminal = response
            return []
        if kind in {"response.output_item.added", "response.output_item.done"}:
            self._require_open(kind)
            index = _index(event, "output_index")
            item = event.get("item")
            complete = kind == "response.output_item.done"
            self._items.observe(index, item, complete=complete)
            if not isinstance(item, dict):
                raise OpenAIResponsesError("invalid_output_item")
            if complete:
                return self._complete_item(index, output_item(item))
            if item["type"] == "function_call":
                return self._tool_header(index, item)
            if item["type"] not in {"message", "reasoning"}:
                raise OpenAIResponsesError("unsupported_output_item")
            return []
        lanes = {
            "response.output_text": ("message", "output_text", "content_index", "text"),
            "response.refusal": ("message", "refusal", "content_index", "refusal"),
            "response.reasoning_text": ("reasoning", "reasoning_text", "content_index", "text"),
            "response.reasoning_summary_text": (
                "reasoning",
                "summary_text",
                "summary_index",
                "text",
            ),
        }
        stem, _, suffix = kind.rpartition(".")
        if stem in lanes and suffix in {"delta", "done"}:
            self._require_open(kind)
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
            self._require_open(kind)
            index = self._event_item(event, "function_call")
            tool = self._tool(index)
            text = (
                tool.arguments.delta(event.get("delta"))
                if suffix == "delta"
                else tool.arguments.finish(event.get("arguments"))
            )
            return _tool_delta(tool.ordinal, arguments=text)
        if kind in {
            "response.created",
            "response.queued",
            "response.in_progress",
            "response.content_part.added",
            "response.content_part.done",
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_part.done",
            "response.output_text.annotation.added",
        }:
            # These notices carry no independent replay items, but still belong
            # to the response lifecycle and must precede its terminal event.
            self._require_open(kind)
        # Unknown event types are extensions, including after response.completed.
        # Ignore them without treating them as output or a completion boundary.
        # Unknown item types inside known output events remain errors above.
        return []

    def _require_open(self, kind: str) -> None:
        if self._terminal is not None or self._finished:
            raise OpenAIResponsesError(
                "duplicate_terminal" if kind == "response.completed" else "output_after_terminal"
            )

    def finish(self) -> list[ProviderStreamEvent]:
        if self._finished:
            raise OpenAIResponsesError("duplicate_terminal")
        if self._terminal is None:
            raise OpenAIResponsesError("missing_terminal")
        self._finished = True
        items = self._items.finish(self._terminal.get("output", []))
        events: list[ProviderStreamEvent] = []
        for index, item in enumerate(items):
            events.extend(self._complete_item(index, output_item(item)))
        if [self._tools[index].ordinal for index in sorted(self._tools)] != list(
            range(len(self._tools))
        ):
            raise OpenAIResponsesError("conflicting_output_item")
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
            raise OpenAIResponsesError("invalid_event")
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
            raise OpenAIResponsesError("invalid_output_item")
        if (tool.call_id and tool.call_id != call_id) or (tool.name and tool.name != name):
            raise OpenAIResponsesError("conflicting_output_item")
        if self._call_ids.get(call_id, index) != index:
            raise OpenAIResponsesError("duplicate_tool_call")
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
        events = []
        keys = set()
        fields = ("content",) if item["type"] == "message" else ("summary", "content")
        for content_field in fields:
            for part_index, part in enumerate(item.get(content_field) or []):
                lane = part["type"]
                key = (index, lane, part_index)
                keys.add(key)
                progress = self._text.setdefault(key, _Text())
                events.extend(
                    _display(
                        lane, progress.finish(part["refusal" if lane == "refusal" else "text"])
                    )
                )
        # Visible reasoning may be streamed without being repeated in the
        # replay item. Preserve the native item rather than synthesizing text.
        if item["type"] == "message" and any(
            key[0] == index and key not in keys for key in self._text
        ):
            raise OpenAIResponsesError("conflicting_output_item")
        return events


def _index(event: dict[str, Any], key: str) -> int:
    value = event.get(key)
    if type(value) is not int or value < 0:
        raise OpenAIResponsesError("invalid_event")
    return value


def _display(lane: str, text: str) -> list[ProviderStreamEvent]:
    if not text:
        return []
    if lane in {"summary_text", "reasoning_text"}:
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
    decoder = OpenAIResponsesStreamDecoder(provider_id=provider_id, model_id=model_id, scope=scope)
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
