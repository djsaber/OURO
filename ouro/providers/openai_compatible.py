from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from ouro.core.messages import MessageRecord
from ouro.core.provider import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderStreamEvent,
    Usage,
)
from ouro.providers.openai_codec import (
    _content_to_text,
    _error_from_response,
    _first_choice,
    _message_to_openai,
    _tool_call_delta_parts,
    _tool_calls_from_message,
    _tool_calls_from_stream_parts,
    _tool_to_openai,
    _usage_from_response,
    _usage_from_response_or_none,
)
from ouro.providers.openai_transport import (
    HttpxOpenAICompatibleTransport,
    OpenAICompatibleTransport,
)


class OpenAICompatibleProvider:
    provider_id = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        provider_id: str | None = None,
        endpoint: str = "/chat/completions",
        timeout: float = 60.0,
        transport: OpenAICompatibleTransport | None = None,
        default_headers: Mapping[str, str] | None = None,
        request_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.provider_id = provider_id or self.provider_id
        self.base_url = base_url.rstrip("/")
        self.endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport or HttpxOpenAICompatibleTransport()
        self.default_headers = dict(default_headers or {})
        self.request_options = dict(request_options or {})

    async def stream(
        self,
        *,
        model: ModelMetadata,
        messages: list[MessageRecord],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ProviderStreamEvent]:
        try:
            stream_json = getattr(self.transport, "stream_json", None)
            if callable(stream_json):
                payload = self._chat_completion_payload(
                    model=model,
                    messages=messages,
                    tools=tools,
                    extra_payload={"stream": True},
                )
                chunks = stream_json(
                    url=f"{self.base_url}{self.endpoint}",
                    headers=self._headers(),
                    payload=payload,
                    timeout=self.timeout,
                )
                async for event in self._events_from_streaming_chunks(chunks):
                    yield event
            else:
                response = await self._post_chat_completion(
                    model=model,
                    messages=messages,
                    tools=tools,
                    extra_payload={"stream": False},
                )
                async for event in self._events_from_response(response):
                    yield event
        except Exception as exc:  # noqa: BLE001 - providers should report errors as stream events.
            yield ProviderStreamEvent(type="error", error=str(exc))

    async def check_connection(self, model: ModelMetadata) -> ProviderCheckResult:
        try:
            response = await self._post_chat_completion(
                model=model,
                messages=[
                    MessageRecord.text(
                        session_id="connection_check",
                        role="user",
                        text="ping",
                    )
                ],
                tools=None,
                extra_payload={"stream": False, "max_tokens": 1},
            )
            async for event in self._events_from_response(response):
                if event.type == "error":
                    raise RuntimeError(event.error or "provider connection check failed")
        except Exception as exc:  # noqa: BLE001 - expose check status without crashing callers.
            return ProviderCheckResult(
                provider_id=self.provider_id,
                ok=False,
                model_id=model.model_id,
                message=str(exc),
            )
        return ProviderCheckResult(
            provider_id=self.provider_id,
            ok=True,
            model_id=model.model_id,
            message="ok",
        )

    async def _post_chat_completion(
        self,
        *,
        model: ModelMetadata,
        messages: list[MessageRecord],
        tools: list[dict[str, Any]] | None,
        extra_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload = self._chat_completion_payload(
            model=model,
            messages=messages,
            tools=tools,
            extra_payload=extra_payload,
        )
        return await self.transport.post_json(
            url=f"{self.base_url}{self.endpoint}",
            headers=self._headers(),
            payload=payload,
            timeout=self.timeout,
        )

    def _chat_completion_payload(
        self,
        *,
        model: ModelMetadata,
        messages: list[MessageRecord],
        tools: list[dict[str, Any]] | None,
        extra_payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model.model_id,
            "messages": [_message_to_openai(message) for message in messages],
            **self.request_options,
            **extra_payload,
        }
        if tools and model.supports_tools:
            payload["tools"] = [_tool_to_openai(tool) for tool in tools]
        return payload

    def _headers(self) -> dict[str, str]:
        headers = dict(self.default_headers)
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _events_from_response(
        self,
        response: Mapping[str, Any],
    ) -> AsyncIterator[ProviderStreamEvent]:
        response_error = _error_from_response(response)
        if response_error:
            yield ProviderStreamEvent(type="error", error=response_error)
            return

        usage = _usage_from_response(response)
        choice = _first_choice(response)
        if choice is None:
            yield ProviderStreamEvent(type="error", error="provider response has no choices")
            return

        message = choice.get("message") or {}
        if not isinstance(message, Mapping):
            yield ProviderStreamEvent(type="error", error="provider response message is invalid")
            return

        content = _content_to_text(message.get("content"))
        if content:
            yield ProviderStreamEvent(type="text_delta", text=content)

        tool_calls = _tool_calls_from_message(message)
        for tool_call in tool_calls:
            yield ProviderStreamEvent(type="tool_call", tool_call=tool_call, usage=usage)

        if not content and not tool_calls:
            finish_reason = choice.get("finish_reason")
            reason = (
                f" finish_reason={finish_reason}"
                if isinstance(finish_reason, str) and finish_reason
                else ""
            )
            yield ProviderStreamEvent(
                type="error",
                error=f"provider returned an empty assistant message{reason}",
            )
            return

        yield ProviderStreamEvent(type="done", usage=None if tool_calls else usage)

    async def _events_from_streaming_chunks(
        self,
        chunks: AsyncIterator[Mapping[str, Any]],
    ) -> AsyncIterator[ProviderStreamEvent]:
        usage: Usage | None = None
        saw_content = False
        tool_call_parts: dict[int, dict[str, str]] = {}

        async for chunk in chunks:
            response_error = _error_from_response(chunk)
            if response_error:
                yield ProviderStreamEvent(type="error", error=response_error)
                return

            chunk_usage = _usage_from_response_or_none(chunk)
            if chunk_usage is not None:
                usage = chunk_usage

            choice = _first_choice(chunk)
            if choice is None:
                continue
            delta = choice.get("delta") or {}
            if not isinstance(delta, Mapping):
                continue

            content = _content_to_text(delta.get("content"))
            if content:
                saw_content = True
                yield ProviderStreamEvent(type="text_delta", text=content)

            for index, part in _tool_call_delta_parts(delta):
                current = tool_call_parts.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                if part.get("id"):
                    current["id"] = part["id"]
                if part.get("name"):
                    current["name"] += part["name"]
                if part.get("arguments"):
                    current["arguments"] += part["arguments"]

        tool_calls = _tool_calls_from_stream_parts(tool_call_parts)
        for tool_call in tool_calls:
            yield ProviderStreamEvent(type="tool_call", tool_call=tool_call, usage=usage)

        if not saw_content and not tool_calls:
            yield ProviderStreamEvent(
                type="error", error="provider returned an empty assistant message"
            )
            return

        yield ProviderStreamEvent(type="done", usage=None if tool_calls else usage)
