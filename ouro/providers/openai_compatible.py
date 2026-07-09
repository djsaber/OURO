from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

import httpx

from ouro.core.messages import MessageRecord
from ouro.providers.base import (
    ModelMetadata,
    ProviderCheckResult,
    ProviderStreamEvent,
    Usage,
)


class OpenAICompatibleTransport(Protocol):
    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]: ...


class HttpxOpenAICompatibleTransport:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        request_headers = {
            "content-type": "application/json",
            "accept": "application/json",
            **headers,
        }
        try:
            response = await self._post(
                url=url,
                headers=request_headers,
                payload=payload,
                timeout=timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text
            raise RuntimeError(
                f"provider HTTP {exc.response.status_code}: {_compact_body(body)}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError("provider request timed out") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"provider connection failed: {exc}") from exc

        if not response.content:
            return {}
        try:
            parsed = response.json()
        except ValueError as exc:
            raise RuntimeError("provider response was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("provider response JSON must be an object")
        return parsed

    async def _post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> httpx.Response:
        if self.client is not None:
            return await self.client.post(
                url,
                headers=dict(headers),
                json=dict(payload),
                timeout=timeout,
            )
        async with httpx.AsyncClient() as client:
            return await client.post(
                url,
                headers=dict(headers),
                json=dict(payload),
                timeout=timeout,
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
        payload: dict[str, Any] = {
            "model": model.model_id,
            "messages": [_message_to_openai(message) for message in messages],
            **self.request_options,
            **extra_payload,
        }
        if tools and model.supports_tools:
            payload["tools"] = [_tool_to_openai(tool) for tool in tools]
        return await self.transport.post_json(
            url=f"{self.base_url}{self.endpoint}",
            headers=self._headers(),
            payload=payload,
            timeout=self.timeout,
        )

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


def _message_to_openai(message: MessageRecord) -> dict[str, Any]:
    role = message.role
    if role == "system_synthetic":
        openai_role = "system"
    elif role == "tool_result":
        openai_role = "tool"
    else:
        openai_role = role

    converted: dict[str, Any] = {
        "role": openai_role,
        "content": message.text_content,
    }
    if openai_role == "assistant":
        tool_calls = message.metadata.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            converted["tool_calls"] = [_tool_call_message_to_openai(item) for item in tool_calls]
    if openai_role == "tool":
        converted["tool_call_id"] = str(message.metadata.get("tool_call_id") or "unknown")
    return converted


def _tool_to_openai(tool: Mapping[str, Any]) -> dict[str, Any]:
    parameters = tool.get("parameters_schema") or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description") or "",
            "parameters": parameters,
        },
    }


def _tool_call_message_to_openai(tool_call: object) -> dict[str, Any]:
    if not isinstance(tool_call, Mapping):
        return {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": "{}"},
        }
    arguments = tool_call.get("arguments") or {}
    return {
        "id": str(tool_call.get("id") or ""),
        "type": "function",
        "function": {
            "name": str(tool_call.get("name") or ""),
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _first_choice(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    return choice if isinstance(choice, Mapping) else None


def _error_from_response(response: Mapping[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, str):
        return error
    if not isinstance(error, Mapping):
        return ""
    message = error.get("message")
    code = error.get("code")
    if isinstance(message, str) and message:
        if isinstance(code, str | int | float) and code:
            return f"{message} ({code})"
        return message
    return json.dumps(dict(error), ensure_ascii=False)


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, Mapping):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _tool_calls_from_message(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_tool_calls = message.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        return []

    tool_calls: list[dict[str, Any]] = []
    for raw_tool_call in raw_tool_calls:
        if not isinstance(raw_tool_call, Mapping):
            continue
        function = raw_tool_call.get("function") or {}
        if not isinstance(function, Mapping):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        tool_calls.append(
            {
                "id": str(raw_tool_call.get("id") or ""),
                "name": name,
                "arguments": _arguments_to_dict(function.get("arguments")),
            }
        )
    return tool_calls


def _arguments_to_dict(arguments: object) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    if isinstance(arguments, str) and arguments.strip():
        parsed = json.loads(arguments)
        if isinstance(parsed, dict):
            return parsed
    return {}


def _usage_from_response(response: Mapping[str, Any]) -> Usage:
    raw_usage = response.get("usage")
    if not isinstance(raw_usage, Mapping):
        return Usage()
    prompt_details = raw_usage.get("prompt_tokens_details") or {}
    cached_tokens = 0
    if isinstance(prompt_details, Mapping):
        cached_tokens = int(prompt_details.get("cached_tokens") or 0)
    return Usage(
        input_tokens=int(raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens") or 0),
        output_tokens=int(
            raw_usage.get("completion_tokens") or raw_usage.get("output_tokens") or 0
        ),
        cache_read_tokens=cached_tokens,
    )


def _compact_body(body: str) -> str:
    compacted = " ".join(body.split())
    return compacted[:500]
